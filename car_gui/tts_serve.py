#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tts_serve.py —— 把板子的 TTS 变成「网络音箱」（零额外依赖，只用标准库）

为什么需要这个东西
------------------
本板**没有任何音频输出硬件**，而且已逐条确认是硬件事实（见 `tts_engine.py` 文件头）：
板载无声卡、HDMI 无 codec 驱动、蓝牙内核里根本没编（`# CONFIG_BT is not set`）、
外接的 PCM2902 是纯采集（USB 描述符里没有 OUT 端点）。

买 USB 音箱当然是最正规的解法，但在音箱到手之前，语音这条路不该停着。

**思路**：声音不必从板子出来。让板子只负责"算"，把算好的音频经网络送到
一台**本来就有喇叭**的设备上播放 —— 你的 PC 浏览器、手机浏览器、平板都行。

    板子（Atlas 200I DK A2）                     播放端（PC / 手机浏览器）
    ┌─────────────────────────┐                 ┌──────────────────────┐
    │  http://ip:8099/        │   ← 打字试听      │  直接出声             │
    │  TtsEngine 现场合成      │                 │                      │
    │  http://ip:8099/listen  │   ← 长轮询取音频   │  当"车的喇叭"常开      │
    └─────────────────────────┘                 └──────────────────────┘

三个页面 / 接口
---------------
| 地址 | 用途 |
|---|---|
| `GET /` | **手动试听页**：输入文字 → 点一下 → PC 出声。用来挑音色、试文案 |
| `GET /listen` | **自动收听页**：常开，车要说话时自动播出。这就是"车的喇叭" |
| `POST /say` | **程序接口**：`{"text":"已急停"}` → 投递播报（给 GUI / KWS 用） |
| `GET /say.wav?text=...` | 直接返回 wav，curl / ffplay / 任意播放器都能拉 |
| `GET /healthz` | 健康检查 JSON |

用法
----
    # 板子上
    cd ~/car_gui
    bash run_py.sh tts_serve.py --port 8099

    # PC / 手机浏览器
    http://192.168.137.100:8099/           # 打字试听
    http://192.168.137.100:8099/listen     # 挂后台当喇叭

    # 命令行投递一句（先在板子上跑，才能在 PC 上听到）
    curl -X POST http://127.0.0.1:8099/say -d '{"text":"已急停"}'

⚠️ 在 PC 上 curl 板子时记得加 `--noproxy '*'`
    PC 上常设着 `http_proxy`（本次实测 `http://127.0.0.1:7896`）。
    curl 会**把发往局域网地址的请求也丢给代理**，于是要么绕远、要么超时。
    正确写法：
        curl --noproxy '*' -o out.wav \
             "http://192.168.137.100:8099/say.wav?text=%E5%B7%B2%E6%80%A5%E5%81%9C"

⚠️ 注意
    · 本脚本**只读标准库**，不需要 pip 装任何东西（sherpa_onnx 是 TTS 引擎用的，
      已经在环境里了）。
    · 首次调用会加载模型（约 3.7 秒），之后常驻内存，单句合成 1~4 秒。
    · 服务监听 0.0.0.0，同局域网内可访问。演示环境是自己的内网，无需鉴权；
      若接到公共网络请自行加访问控制。
    · **必须保证只有一个进程在合成** —— 所以合成放在单线程worker里串行做，
      多个请求排队，不会互相抢 CPU（本板只有 4 核 1.0GHz）。
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tts_engine as T                                    # noqa: E402

MAX_TEXT_LEN = 200


# ══════════════════════════════════════════════════════════════════════
#  播报广播站：投递文本 → 后台合成 → 前端轮询取走
# ══════════════════════════════════════════════════════════════════════
class Speaker(object):
    """
    一个极简的"播报广播站"。

    为什么要有它：合成要 1~4 秒，如果直接在 HTTP 请求里算，
    投递方（GUI / KWS 回调）会被卡住。所以**投递即返回**，
    合成交给常驻后台线程串行做，播放端用长轮询来取。
    """

    def __init__(self, eng=None, speaker=0, speed=1.0, keep=40):
        self._eng = eng
        self._speaker = int(speaker)
        self._speed = float(speed)
        self._keep = keep
        self._q = Queue()
        self._lock = threading.Lock()
        self._items = {}                 # seq -> {"bytes","text","err"}
        self._seq = 0
        # eng=None → **广播模式**：自己不再合成，音频由外部（car_gui_core 的
        # SpeakQueue）通过 push() 送进来。见 start_background()。
        self._thread = None
        if eng is not None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    @property
    def seq(self):
        with self._lock:
            return self._seq

    def push(self, rate, data, text=""):
        """
        广播模式：直接塞入一段**已经合成好的**音频（供浏览器播放）。

        由 `TtsEngine` 的 sink 回调调用，所以**跑在播报线程里，必须快** ——
        这里只做"编码成 wav + 存字典"，几毫秒。
        """
        wav = T.pcm_to_wav_bytes(rate, data)
        if not wav:
            return 0
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._items[seq] = {"bytes": wav, "text": text or "", "err": ""}
            self._trim()
        return seq

    def submit(self, text):
        """投递一句文本让它去合成（需要 eng）。广播模式返回 0。"""
        text = (text or "").strip()[:MAX_TEXT_LEN]
        if not text or self._eng is None:
            return 0
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._items[seq] = {"bytes": None, "text": text, "err": ""}
            self._trim()
        self._q.put((seq, text))
        return seq

    def _trim(self):
        """只保留最近 keep 条，防止内存无限增长。"""
        if len(self._items) <= self._keep:
            return
        for s in sorted(self._items)[:-self._keep]:
            self._items.pop(s, None)

    def _loop(self):
        while True:
            seq, text = self._q.get()
            wav, err = None, ""
            try:
                sr, data = self._eng.synth(text, self._speaker, self._speed)
                if data is None:
                    err = "合成失败"
                else:
                    wav = T.pcm_to_wav_bytes(sr, data)
            except Exception as e:
                err = str(e)
            with self._lock:
                it = self._items.get(seq)
                if it is not None:
                    it["bytes"], it["err"] = wav, err

    def item(self, seq):
        with self._lock:
            it = self._items.get(seq)
            return dict(it) if it else None

    def next_ready(self, since):
        """返回 > since 的、**已经合成好**的最小序号；没有则 0。"""
        with self._lock:
            cand = [s for s, it in self._items.items()
                    if s > since and it["bytes"]]
        return min(cand) if cand else 0

    def pending(self):
        with self._lock:
            return sum(1 for it in self._items.values() if not it["bytes"])


# ══════════════════════════════════════════════════════════════════════
#  页面
# ══════════════════════════════════════════════════════════════════════
_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--tx:#1f2328;--dim:#6b7280;
 --ac:#2563eb;--ac2:#1d4ed8;--ok:#15803d;--err:#b91c1c}
*{box-sizing:border-box}
body{margin:0;padding:18px;font:15px/1.6 -apple-system,BlinkMacSystemFont,
 "Segoe UI","Noto Sans CJK SC","Microsoft YaHei",sans-serif;
 background:var(--bg);color:var(--tx)}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:19px;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:16px;margin-bottom:14px}
label{display:block;font-size:13px;color:var(--dim);margin:0 0 6px}
textarea,input[type=number]{width:100%;border:1px solid var(--line);border-radius:8px;
 padding:10px;font:inherit;background:#fff;color:var(--tx)}
textarea{min-height:78px;resize:vertical}
.row{display:flex;gap:12px;margin-top:12px}
.row>div{flex:1}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 4px}
.chip{border:1px solid var(--line);background:#fff;border-radius:999px;
 padding:5px 12px;font-size:13px;cursor:pointer;color:var(--dim)}
.chip:hover{border-color:var(--ac);color:var(--ac)}
button{width:100%;margin-top:12px;padding:12px;border:0;border-radius:9px;
 font:inherit;font-weight:600;cursor:pointer;background:var(--ac);color:#fff}
button:hover{background:var(--ac2)}
button.ghost{background:#fff;color:var(--ac);border:1px solid var(--ac)}
button.ghost:hover{background:#eff5ff}
button:disabled{opacity:.5;cursor:default}
.log{font-size:13px;color:var(--dim);white-space:pre-wrap;min-height:22px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;
 background:#d1d5db;margin-right:7px;vertical-align:1px}
.dot.on{background:var(--ok)}
.dot.err{background:var(--err)}
a{color:var(--ac)}
"""

PAGE_MAIN = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>小车语音 · 板载 TTS</title><style>__CSS__</style></head><body>
<div class="wrap">
<h1>小车语音 · 板载 TTS</h1>
<p class="sub">声音由 <b>Atlas 200I DK A2</b> 现场合成（离线 VITS），
经网络送到这里播放。本板没有喇叭，所以借你的浏览器当喇叭。</p>

<div class="card">
  <label>要说的内容</label>
  <textarea id="text">已急停</textarea>
  <div class="chips" id="chips"></div>
  <div class="row">
    <div><label>说话人编号</label><input id="spk" type="number" value="__SPEAKER__" min="0" max="200"></div>
    <div><label>语速</label><input id="spd" type="number" value="1.0" step="0.05" min="0.5" max="1.5"></div>
  </div>
  <button id="play">🔊 直接试听</button>
  <button id="push" class="ghost">📣 推送到「收听页」</button>
</div>

<div class="card">
  <div class="log"><span class="dot" id="dot"></span><span id="log">就绪</span></div>
  <p class="sub" style="margin:12px 0 0">想让车"隔着房间"说话？
  另开一个 <a href="/listen">/listen</a> 页面挂着，它会在车需要说话时自动播出。</p>
</div>
</div>
<script>
const $=s=>document.querySelector(s);
const PRESETS=["已急停","开始前进","小车已停止","前方有障碍，已停止",
  "电量不足，请充电","系统就绪","左转，请注意"];
const chips=$('#chips');
PRESETS.forEach(t=>{const b=document.createElement('button');
  b.className='chip';b.textContent=t;b.type='button';
  b.onclick=()=>{$('#text').value=t;};chips.appendChild(b);});
function log(msg,cls){$('#log').textContent=msg;
  $('#dot').className='dot'+(cls?' '+cls:'');}
async function listen(){
  const t=$('#text').value.trim();
  if(!t){log('先写点什么','err');return;}
  log('合成中…（板子 1~4 秒）');
  $('#play').disabled=true;
  try{
    const p=new URLSearchParams({text:t,speaker:$('#spk').value,speed:$('#spd').value});
    const r=await fetch('/say.wav?'+p);
    if(!r.ok){log('失败：'+await r.text(),'err');return;}
    const b=await r.blob();
    await new Audio(URL.createObjectURL(b)).play();
    log('已播放：'+t,'on');
  }catch(e){log('出错：'+e,'err');}
  finally{$('#play').disabled=false;}
}
$('#play').onclick=listen;
$('#push').onclick=async()=>{
  const t=$('#text').value.trim();
  if(!t){log('先写点什么','err');return;}
  const r=await fetch('/say',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
  const j=await r.json();
  log(j.seq?('已投递 #'+j.seq+'，等待 /listen 页面播出'):'投递失败','on');
};
</script></body></html>"""

PAGE_LISTEN = """<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>小车喇叭 · 收听中</title><style>__CSS__</style></head><body>
<div class="wrap">
<h1>小车喇叭</h1>
<p class="sub">这一页就是"车的喇叭"。点下面的按钮开始，然后把它挂在后台
（手机、平板、另一台电脑都行），车要说话时会自动播出。</p>

<div class="card">
  <button id="toggle">▶ 开始收听</button>
  <div class="log" style="margin-top:14px">
    <span class="dot" id="dot"></span><span id="log">未开始</span>
  </div>
</div>

<div class="card">
  <label>状态</label>
  <div class="log" id="stat">已播报 0 条</div>
  <p class="sub" style="margin:12px 0 0">
  回到 <a href="/">手动试听页</a> 打字，或点「推送到收听页」，声音就会出现在这里。</p>
</div>
</div>
<script>
const $=s=>document.querySelector(s);
let since=0,on=false,n=0,timer=null;
function log(m,c){$('#log').textContent=m;$('#dot').className='dot'+(c?' '+c:'');}
function stat(){ $('#stat').textContent='已播报 '+n+' 条　（当前序号 '+since+'）'; }
async function loop(){
  while(on){
    try{
      const r=await fetch('/next?since='+since+'&wait=20');
      const j=await r.json();
      if(!on)break;
      if(j.seq>since){
        since=j.seq;
        const a=new Audio(j.url);
        await a.play().catch(()=>{});
        // 等这一句放完再取下一句，避免两条播报叠在一起
        await new Promise(res=>{a.onended=res;setTimeout(res,15000);});
        n++;stat();log('已播出：'+(j.text||''),'on');
      }
    }catch(e){ await new Promise(r=>setTimeout(r,1200)); }
  }
}
$('#toggle').onclick=async()=>{
  if(on){on=false;$('#toggle').textContent='▶ 开始收听';log('已停止');return;}
  // 浏览器要求先有用户手势才能放声音，所以必须由这个点击来启动
  on=true;$('#toggle').textContent='■ 停止收听';
  log('收听中…','on');
  try{await new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=').play();}catch(e){}
  // 对齐到板子当前序号：从"现在"开始听，不回放之前的播报
  try{const r=await fetch('/seq');const j=await r.json();
      if(j.seq>since)since=j.seq;stat();}catch(e){}
  loop();
};
stat();
</script></body></html>"""


# ══════════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    server_version = "car-tts/1.0"
    protocol_version = "HTTP/1.1"

    # 由 build_server 注入
    engine = None
    speaker = None
    speaker_id = 0
    boot = 0.0

    def log_message(self, fmt, *a):
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % a))

    # ── 回包helpers ────────────────────────────────────────────────
    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _html(self, page):
        body = (page.replace("__CSS__", _CSS)
                    .replace("__SPEAKER__", str(self.speaker_id)))
        self._send(200, body, "text/html; charset=utf-8")

    # ── GET ────────────────────────────────────────────────────────
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            return self._html(PAGE_MAIN)
        if p == "/listen":
            return self._html(PAGE_LISTEN)
        if p == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if p == "/healthz":
            probe = T.output_probe()
            return self._json({
                "ok": True,
                "engine_ready": bool(self.engine and self.engine.ready),
                "speakers": self.engine.num_speakers if self.engine else 0,
                "sample_rate": getattr(self.engine, "sample_rate", 0),
                "uptime_sec": round(time.time() - self.boot, 1),
                "queue_pending": self.speaker.pending(),
                "seq": self.speaker.seq,
                "board_audio_output": probe["ok"],
                "alsa_playback_devices": len(probe["alsa"]),
            })

        if p == "/say.wav":
            text = (q.get("text") or ["已急停"])[0].strip()[:MAX_TEXT_LEN]
            try:
                spk = int((q.get("speaker") or ["0"])[0])
            except Exception:
                spk = 0
            try:
                spd = float((q.get("speed") or ["1.0"])[0])
            except Exception:
                spd = 1.0
            if not text:
                return self._send(400, "text 为空")
            t0 = time.time()
            sr, data = self.engine.synth(text, spk, spd)
            if data is None:
                return self._send(500, "合成失败：%s" % text)
            wav = T.pcm_to_wav_bytes(sr, data)
            print("  [合成] %.2fs  「%s」→ %.2fs 音频" % (
                time.time() - t0, text, len(data) / float(sr)))
            return self._send(200, wav, "audio/wav",
                              {"X-Synth-Sec": "%.2f" % (time.time() - t0)})

        if p == "/seq":
            # 让 /listen 页面在开始时拿一个"当前进度"锚点，从这之后开始听，
            # 不回放历史播报。（早先把这段逻辑塞在 /next 里，语义很绕，
            # 而且会白等一次超时才返回 —— 拆出来干净得多。）
            return self._json({"seq": self.speaker.seq})

        if p == "/next":
            try:
                since = int((q.get("since") or ["0"])[0])
            except Exception:
                since = 0
            try:
                wait = min(60.0, float((q.get("wait") or ["20"])[0]))
            except Exception:
                wait = 20.0
            end = time.time() + wait
            while True:
                s = self.speaker.next_ready(since)
                if s:
                    it = self.speaker.item(s) or {}
                    return self._json({"seq": s, "text": it.get("text", ""),
                                       "url": "/audio/%d.wav" % s})
                if time.time() >= end:
                    return self._json({"seq": since, "text": "", "url": ""})
                time.sleep(0.1)

        if p.startswith("/audio/"):
            tail = p[len("/audio/"):]
            if not tail.endswith(".wav"):
                return self._send(404, "not found")
            try:
                seq = int(tail[:-4])
            except Exception:
                return self._send(404, "bad seq")
            it = self.speaker.item(seq)
            if not it or not it.get("bytes"):
                return self._send(404, "音频还没合成好")
            return self._send(200, it["bytes"], "audio/wav")

        return self._send(404, "not found")

    # ── POST ───────────────────────────────────────────────────────
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        raw = self.rfile.read(n) if n else b""
        ct = self.headers.get("Content-Type", "")

        if u.path != "/say":
            return self._send(404, "not found")

        data = {}
        try:
            if "json" in ct:
                data = json.loads(raw.decode("utf-8", "replace") or "{}")
            else:
                for k, v in urllib.parse.parse_qs(
                        raw.decode("utf-8", "replace")).items():
                    data[k] = v[0]
        except Exception as e:
            return self._json({"seq": 0, "error": "解析失败：%s" % e}, 400)

        text = (data.get("text") or "").strip()
        if not text:
            return self._json({"seq": 0, "error": "text 为空"}, 400)

        seq = self.speaker.submit(text)
        if seq == 0:
            # 广播模式（音频是 car_gui_core 推进来的）—— 这里没有合成器
            return self._json({
                "seq": 0,
                "error": "本服务现在是「广播模式」：它只负责把小车自己合成的"
                         "播报送到浏览器。想手动试听请用 GET /say.wav?text=…"}, 409)
        print("  [投递] #%d 「%s」" % (seq, text))
        return self._json({"seq": seq, "text": text,
                           "audio": "/audio/%d.wav" % seq})


def build_server(eng, spk, port, host, speaker_id=0):
    h = type("BoundHandler", (Handler,), {"engine": eng, "speaker": spk,
                                          "speaker_id": speaker_id,
                                          "boot": time.time()})
    srv = ThreadingHTTPServer((host, port), h)
    srv.daemon_threads = True
    srv.allow_reuse_address = True
    return srv


def local_ip():
    """猜一个局域网 IP 用于打印提示（不会真的发包）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_background(engine, port=8099, host="0.0.0.0", log=None):
    """
    用**已有的** TtsEngine 起一个后台"网络音箱" —— **不重复加载模型**。

    这是给 `car_gui_core` 用的：本板没有本地播放设备，播报合成完如果只能
    丢在本地就等于白做。这里把 `bc.push` 注册成 engine 的 sink，
    之后**引擎每合成一句，都会自动出现在浏览器的 /listen 页面上**。

    返回 `(server, broadcaster, 错误字符串)`；成功时错误为空串。
    """
    log = log or (lambda s: None)
    bc = Speaker(eng=None, keep=30)
    try:
        engine.add_sink(lambda sr, data, text: bc.push(sr, data, text))
    except Exception as e:
        return None, None, "注册 sink 失败：%s" % e
    try:
        srv = build_server(engine, bc, port, host, speaker_id=0)
    except OSError as e:
        return None, None, "端口 %d 起不来：%s" % (port, e)
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="tts-web-speaker").start()
    log("🔊 网络音箱已启动：http://%s:%d/listen  （板子没喇叭，用它当喇叭）"
        % (local_ip(), port))
    return srv, bc, ""


# ══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="把板载 TTS 变成网络音箱")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="0.0.0.0",
                    help="监听地址（默认 0.0.0.0，局域网可访问）")
    ap.add_argument("--speaker", type=int, default=None,
                    help="默认说话人（不给则用该模型实测好用的那个；"
                         "多语言模型的编号 0 是英语音色，别乱用）")
    ap.add_argument("--speed", type=float, default=1.0, help="默认语速")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--dir", default=None, help="TTS 模型目录")
    args = ap.parse_args()

    print("=" * 70)
    print("  板载 TTS · 网络音箱")
    print("=" * 70)

    probe = T.output_probe()
    if probe["ok"]:
        print("  提示：本机其实**有**输出设备，直接 --play 更好，不必用这个服务。")
    else:
        print("  本机无音频输出设备（硬件限制，见 tts_engine.py 文件头），")
        print("  所以声音将送到浏览器播放。")

    eng = T.TtsEngine(log=lambda s: print("  [TTS] %s" % s))
    print()
    print("  正在加载 TTS 模型（约 3.7 秒）…")
    ok, msg = eng.load(args.dir, args.threads)
    print("  %s %s" % ("✅" if ok else "❌", msg))
    if not ok:
        print("\n  加载失败。先跑 `bash run_py.sh tts_engine.py --check` 看诊断。")
        return 1

    if args.speaker is None:
        args.speaker = T.default_speaker(eng._dir)
    rng = T.zh_speaker_range(eng._dir)
    if rng and not (rng[0] <= args.speaker <= rng[1]):
        print("  ⚠️  说话人 %d 不在实测能读中文的区间 %d~%d 内，可能带外语口音。"
              % (args.speaker, rng[0], rng[1]))

    spk = Speaker(eng, args.speaker, args.speed)
    srv = build_server(eng, spk, args.port, args.host, args.speaker)

    ip = local_ip()
    print()
    print("  服务已启动　Ctrl+C 停止")
    print("  ─────────────────────────────────────────────")
    print("  手动试听页   http://%s:%d/" % (ip, args.port))
    print("  自动收听页   http://%s:%d/listen" % (ip, args.port))
    print("  健康检查     http://%s:%d/healthz" % (ip, args.port))
    print("  ─────────────────────────────────────────────")
    print("  挑音色：默认说话人 %d，共 %d 个。试听页可直接改编号。"
          % (args.speaker, eng.num_speakers))
    if rng:
        print("           ⚠️ 多语言模型：只有 %d~%d 是中文音色，0 是英语音色。"
              % (rng[0], rng[1]))
    print("  提示：/listen 页面需要**点一下按钮**才允许出声（浏览器自动播放限制）。")
    print("=" * 70)
    sys.stdout.flush()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
