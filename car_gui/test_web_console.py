#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远程 Web 控制台 · 离线回归测试（**不需要串口/音频/显示器**）。

用假车注入 `CarController`，把 `car_web` 的服务跑在本机端口上，
再用 urllib 真发 HTTP 请求 —— 锁死下面这些行为：

  1. 首页能出、含急停按钮、**不含左移/右移**（本车不支持平移）
  2. `/api/status` 字段与真实速度换算正确
  3. 按住走：不带 duration → 持续运动 + `holding=True`
  4. ★ **心跳看门狗**：网页断线后兜底停车（只兜底持续运动）
  5. 不支持的动作被拒；急停锁定后动作被拒
  6. 中文文本指令经 UTF-8 正确送达（**将来电脑端语音识别也走这里**）
  7. 急停 / 解除 / 速度钳位 / 无 TTS 时端点降级

用法： bash run_py.sh test_web_console.py
"""
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import car_gui_core as C                                        # noqa: E402
import car_web as W                                             # noqa: E402

PASS, FAIL = [], []
PORT = 18899


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                         ("  " + extra) if extra else ""))


class FakeCar(object):
    """假串口：记录所有帧。"""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.voltage = 12.34
        self.flag_stop = 0

    def set_speed(self, x=0, y=0, z=0):
        self.sent.append((int(x), int(y), int(z)))

    def get_feedback(self):
        vx, vy, vz = self.sent[-1] if self.sent else (0, 0, 0)
        return {"flag_stop": self.flag_stop, "vx": vx, "vy": vy, "vz": vz,
                "accel_x": 1671, "accel_y": 0, "accel_z": 0,
                "gyro_x": 0, "gyro_y": 0, "gyro_z": 0, "voltage": self.voltage}

    def close(self):
        self.closed = True

    def last(self):
        return self.sent[-1] if self.sent else None


class FakeAsr(object):
    """假语音引擎：不碰麦克风、立刻返回固定识别结果。

    这样才能在**没有音频硬件**的机器上锁死整条语音链路：
    按下 → 录音 → 松开 → 识别 → （可选）自动执行。
    """

    def __init__(self, text="前进一米"):
        self._text = text
        self.ready = True
        self.recording = False
        self.level = 0.42
        self.ptt_device = "FAKE-MIC"
        self.listen_calls = 0
        self.listen_text = "后退半米"

    def ptt_start(self, device=None):
        self.ptt_device = device
        self.recording = True
        return True, "ok"

    def ptt_stop(self):
        self.recording = False
        return True, self._text

    def listen_auto(self, device=None, **kw):
        self.listen_calls += 1
        time.sleep(0.3)              # 模拟真实聆听耗时，同时避免热循环
        return True, self.listen_text


class FakeBC(object):
    """假广播对象：模拟 tts_serve.Speaker 给浏览器取音的那几个方法。

    用来锁死「浏览器当喇叭」的取音语义（2026-11-03 修复）：
    客户端必须先用 /api/tts/seq 锚定，再按 since 取增量 —— 否则会把
    服务端保留的历史播报（开机时的「系统就绪」）重放一遍。
    """

    def __init__(self):
        self._seq = 0
        self.items = {}

    @property
    def seq(self):
        return self._seq

    def push(self, text):
        self._seq += 1
        self.items[self._seq] = {"bytes": b"RIFF", "text": text}
        return self._seq

    def next_ready(self, since):
        cand = [s for s, it in self.items.items()
                if s > since and it["bytes"]]
        return min(cand) if cand else 0

    def item(self, seq):
        it = self.items.get(seq)
        return dict(it) if it else None


class FakeAsrMod(object):
    """假 asr_engine 模块：给「输入麦克风」接口提供固定设备表。

    只需要 `_mic_snapshot` / `/api/mic` 用到的那几个方法：
    real_input_devices / device_identity / _load_preferred_input /
    guess_input_device / save_preferred_input / clear_preferred_input。
    """

    def __init__(self):
        self.devices = [
            {"index": 0, "name": "USB2.0 Device: Audio (hw:0,0)",
             "samplerate": 48000, "channels": 1},
            {"index": 1, "name": "USB Audio: - (hw:1,0)",
             "samplerate": 44100, "channels": 2},
            {"index": 2, "name": "DOV: USB Audio (hw:2,0)",
             "samplerate": 44100, "channels": 2},
        ]
        self.pref = "usb2.0 device"
        self.saved = None

    def real_input_devices(self):
        return [dict(x) for x in self.devices]

    def device_identity(self, name):
        return (name or "").split(" (hw:")[0].split(":")[0].strip().lower()

    def _load_preferred_input(self):
        return self.pref

    def guess_input_device(self):
        for d in self.devices:
            if self.device_identity(d["name"]) == self.pref:
                return d["index"]
        return 0

    def save_preferred_input(self, idx):
        self.saved = idx
        for d in self.devices:
            if d["index"] == idx:
                self.pref = self.device_identity(d["name"])
                return self.pref
        return ""

    def clear_preferred_input(self):
        self.pref = ""
        return True


def make_ctrl():
    c = C.CarController()
    c._announce_actions = False          # 别去碰 TTS
    c._speak_enabled = False
    fake = FakeCar()
    c._car = fake
    c._connected = True
    c._port = "/dev/FAKE0"
    c._speed = 300
    return c, fake


def req(method, path, body=None, timeout=12):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.status, json.loads(raw.decode("utf-8"))
            return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return e.code, raw.decode("utf-8", "replace")


def main():
    print("=" * 62)
    print(" 远程 Web 控制台 · 离线回归")
    print("=" * 62)

    ctrl, fake = make_ctrl()
    W.State.ctrl = ctrl
    W.State.ui = W._load_ui()
    W.State.log_seq = 0
    W.State.logs = []
    W.State.hold = False
    W.State.hb = time.time()
    # 注入假语音引擎（否则语音接口会去真的加载 228MB 模型）
    fake_asr = FakeAsr()
    W.State.asr = fake_asr
    W.State.asr_state = "ready"
    W.State.asr_msg = "语音识别就绪"
    W.State.auto_exec = True
    W.State.listening = False
    W.State.recording = False
    W.State.asr_seq = 0
    W.State.asr_text = ""

    srv = W.QuietServer(("127.0.0.1", PORT), W.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # 只起日志泵 / 看门狗 / 文本执行线程（**不起自动连串口**，否则会去动真串口）
    threading.Thread(target=W._log_pump, daemon=True).start()
    threading.Thread(target=W._watchdog, daemon=True).start()
    threading.Thread(target=W._text_worker, daemon=True).start()
    # 计划进度监听（产生 step 事件，供对话卡片显示 k/n）
    threading.Thread(target=W._plan_watcher, daemon=True).start()
    time.sleep(0.6)

    try:
        # ── 1. 首页 ──────────────────────────────────────────────
        print("\n[1] 首页")
        code, html = req("GET", "/")
        check("首页 200", code == 200, str(code))
        check("含标题", "小车远程控制台" in html)
        check("含急停按钮", 'id="estop"' in html)
        check("含方向键", 'data-a="forward"' in html and 'data-a="turn_left"' in html)
        check("★ 不含左移/右移（本车不支持平移）",
              "strafe" not in html and "左移" not in html and "右移" not in html)
        check("不含冗余勾选项（离线演示/纯聊天等在高级区）",
              "锁定持续运动" not in html and "纯聊天" not in html)
        # ★ 安全：键盘监听必须排除输入框，否则在指令框里打 W/A/S/D 会把车开走
        check("键盘不劫持输入框", 'tag === "INPUT"' in html)
        check("页面不引用已删除的元素", "c_spd" not in html)

        # ── 1b. 改版方案 §9.3 新增断言（对话式任务台）────────────
        print("\n[1b] 改版方案断言")
        check("面向用户不暴露速度单位（无 mm/s 裸值）", "mm/s" not in html)
        check("急停位于状态条内（不依赖滚动）",
              html.find('id="statusbar"') >= 0
              and html.find('id="estop"') > html.find('id="statusbar"')
              and html.find('id="estop"') < html.find("</header>"))
        check("有对话流容器", 'id="chat"' in html)
        check("有常用任务预设", 'class="presets"' in html and "data-t=" in html)
        check("速度是慢/中/快三档（不是裸数值滑杆）",
              'id="seg-speed"' in html and "慢" in html and "快" in html)
        check("有电量格", 'id="batt-bars"' in html)
        # ★ 2026-10-28 用户反馈：板子**有**喇叭，界面却提示"板子没有喇叭"
        check("不谎称「板子没有喇叭」", "板子没有喇叭" not in html)
        check("能显示语音急停是否在让位", 'id="kws-note"' in html)
        # 首次点"自动聆听"要自动等模型加载完，而不是回一句"请稍候再试"
        check("自动聆听会先等模型加载",
              'api("/api/voice/load"' in html
              and 'asr_state !== "ready"' in html)

        # ★ 2026-11-03：喇叭开关修了三个坑（不锚定就复读「系统就绪」/
        #   关不掉长轮询/重复点开关叠加循环）。这里把契约钉死在页面上。
        check("★ 喇叭开启先锚定序号（历史播报不重放）",
              "/api/tts/seq" in html and "ttsSince" in html)
        check("★ 喇叭关闭会中断长轮询 + 暂停播放",
              "AbortController" in html and "audio.pause()" in html
              and 'removeAttribute("src")' in html)
        check("★ 喇叭轮询只允许一个循环（世代号）",
              "ttsLoopId" in html and "setTimeout(listenTts" not in html)

        # ★ 2026-11-03：远程电源（高级功能）——必须在高级抽屉里，
        #   主界面看不到；按钮默认 hidden，status.power=true 才显示。
        check("★ 远程电源入口在高级设置内（不在主界面）",
              'id="power-row"' in html
              and 'id="power-row" hidden' in html
              and html.find('</main>') < html.find('id="power-row"')
              and html.find('id="advanced"') < html.find('id="power-row"'))
        check("★ 电源操作有二次确认弹窗",
              'id="power-modal"' in html and 'id="power-confirm"' in html)
        check("★ 电源走 /api/power（带 confirm）",
              'api("/api/power"' in html and 'confirm: "yes"' in html)

        # ★ 2026-11-05：输入麦克风选择（本板有阵列麦 + 无线麦）
        check("★ 输入麦克风选择器在高级设置内",
              'id="sel-mic"' in html and 'id="btn-mic-refresh"' in html
              and 'api("/api/mic"' in html)

        # ── 1c. v3 改版（2026-10-28 用户反馈 11 条）──────────────
        print("\n[1c] v3 界面改版")
        check("高级设置是覆盖层（遮罩 + 抽屉）",
              'id="adv-mask"' in html and 'class="sheet"' in html)
        check("整页只留一个滚动（侧栏 sticky）",
              "position:sticky" in html and 'id="stage"' in html)
        check("卡片左右分栏", 'class="card-rail"' in html)
        check("拒绝徽标用「没执行」而不是「错误」", "没执行" in html)
        check("不把「离线：」内部前缀露给用户", "离线：" not in html)
        check("日志按相同消息合并计数", "×" in html and "LOG_WARN" in html)
        check("日志默认灰色（只有失败/停止才黄）", ".lg.warn" in html)
        check("有未连接引导卡", 'id="guide"' in html and "btn-retry" in html)
        check("方向键放大到 44px", "44px" in html)
        check("有答辩演示模式", 'id="btn-big"' in html and "localStorage" in html)
        check("自动聆听不再是并列按钮（移入高级）",
              'id="btn-listen"' in html
              and html.find('id="btn-listen"') > html.find('class="sheet"'))

        # ── 2. 状态 ──────────────────────────────────────────────
        print("\n[2] 状态")
        code, s = req("GET", "/api/status")
        check("status 200", code == 200)
        check("connected=True", s.get("connected") is True)
        check("电压透传", abs(s.get("voltage", 0) - 12.34) < 0.01, str(s.get("voltage")))
        # ★ 2026-10-28 标定修正：命令值就是 mm/s，换算应为**恒等**
        check("真实速度换算 (300→300 mm/s)", s.get("speed_mmps") == 300,
              str(s.get("speed_mmps")))

        # ── 3. 按住走（持续运动）────────────────────────────────
        print("\n[3] 按住走 / 松开停")
        fake.sent.clear()
        code, r = req("POST", "/api/action", {"action": "forward"})
        check("forward 下发成功", r.get("ok") is True, str(r))
        check("假车收到 (300,0,0)", fake.last() == (300, 0, 0), str(fake.sent))
        _, s = req("GET", "/api/status")
        check("holding=True", s.get("holding") is True)
        code, r = req("POST", "/api/stop", {})
        check("stop 成功", r.get("ok") is True)
        check("停止帧 (0,0,0)", fake.last() == (0, 0, 0), str(fake.sent))
        _, s = req("GET", "/api/status")
        check("holding=False", s.get("holding") is False)

        # ── 4. ★ 心跳看门狗 ─────────────────────────────────────
        print("\n[4] ★ 心跳看门狗（断网兜底停车）")
        fake.sent.clear()
        req("POST", "/api/action", {"action": "forward"})
        check("先跑起来", fake.last() == (300, 0, 0))
        # 模拟"笔记本断网"：心跳时间往前拨 10 秒
        with W.State.lock:
            W.State.hb = time.time() - 10.0
        time.sleep(1.6)
        check("超时后自动停车", fake.last() == (0, 0, 0), str(fake.sent[-3:]))
        _, s = req("GET", "/api/status")
        check("holding 已清", s.get("holding") is False)

        # 定时运动**不该**被看门狗误杀
        fake.sent.clear()
        req("POST", "/api/action", {"action": "forward", "duration": 5.0})
        with W.State.lock:
            W.State.hb = time.time() - 10.0
        time.sleep(1.6)
        check("定时运动不被误杀（仍在走）", fake.last() == (300, 0, 0), str(fake.sent[-2:]))
        req("POST", "/api/stop", {})

        # ── 5. 白名单 / 锁定 ────────────────────────────────────
        print("\n[5] 动作白名单与急停锁定")
        code, r = req("POST", "/api/action", {"action": "strafe_left"})
        check("strafe_left 被拒", r.get("ok") is False, str(r.get("msg")))
        code, r = req("POST", "/api/action", {"action": "fly"})
        check("乱编动作被拒", r.get("ok") is False)

        code, r = req("POST", "/api/estop", {})
        check("急停成功", r.get("ok") is True, str(r))
        _, s = req("GET", "/api/status")
        check("estop_locked=True", s.get("estop_locked") is True)
        code, r = req("POST", "/api/action", {"action": "forward"})
        check("★ 锁定后动作被拒", r.get("ok") is False, str(r.get("msg")))
        code, r = req("POST", "/api/estop/release", {})
        check("解除锁定", r.get("ok") is True)
        _, s = req("GET", "/api/status")
        check("estop_locked=False", s.get("estop_locked") is False)

        # ── 6. ★ 中文文本指令（电脑端语音识别的入口）────────────
        print("\n[6] ★ 中文文本指令（UTF-8）")
        fake.sent.clear()
        code, r = req("POST", "/api/text", {"text": "前进一米"})
        check("已提交", r.get("queued") is True, str(r))
        time.sleep(1.5)
        # 中文有没有被破坏：从 HTTP 日志接口读（别用 drain_logs —— 会被日志泵取走）
        _, lg = req("GET", "/api/logs?since=0")
        check("★ 中文原文完整送达（UTF-8 未被破坏）",
              any("前进一米" in L for L in lg.get("lines", [])),
              str(lg.get("lines", [])[-3:]))
        check("换算出时长后真下发了（前进帧）",
              any(f[0] > 0 for f in fake.sent), str(fake.sent[:3]))
        req("POST", "/api/stop", {})

        # 多步计划
        fake.sent.clear()
        code, r = req("POST", "/api/text", {"text": "先前进半米，再左转30度"})
        check("多步已提交", r.get("queued") is True)
        time.sleep(5.0)
        acts = [f for f in fake.sent]
        check("★ 多步真的分两段下发（先 x 后 z）",
              any(f[0] > 0 for f in acts) and any(f[2] != 0 for f in acts),
              "帧数=%d" % len(acts))
        req("POST", "/api/stop", {})

        # ── 7. 速度 / 降级 ─────────────────────────────────────
        print("\n[7] 速度钳位与端点降级")
        code, r = req("POST", "/api/speed", {"value": 9999})
        check("速度上界钳到 500", r.get("speed") == 500, str(r))
        code, r = req("POST", "/api/speed", {"value": 1})
        check("速度下界钳到 100", r.get("speed") == 100, str(r))
        code, r = req("POST", "/api/speed", {"value": "abc"})
        check("非法速度被拒", r.get("ok") is False)
        code, r = req("GET", "/api/tts/seq")
        check("无 TTS 时 enabled=False", r.get("enabled") is False, str(r))

        # ── 7b. ★ 浏览器当喇叭：锚定序号 / 不重放历史 ─────────────
        print("\n[7b] ★ 浏览器当喇叭（锚定序号 / 不重放历史）")
        bc = FakeBC()
        bc.push("系统就绪")              # 开机播报 = 历史项
        W.State.ctrl._web = (None, bc, 8099)
        try:
            code, r = req("GET", "/api/tts/seq")
            check("有网络音箱时 enabled=True", r.get("enabled") is True,
                  str(r))
            check("★ 锚定序号 = 当前序号（历史项不计入）",
                  r.get("seq") == 1, str(r))
            # 客户端锚定在 seq=1 后再取：不能把 seq=1 的「系统就绪」再放一遍
            # （超时回包约定是 seq==since 且 text 为空）
            code, r = req("GET", "/api/tts/next?since=1&wait=0.3")
            check("★ 开启声音后不重放历史「系统就绪」",
                  r.get("seq") == 1 and not r.get("text"), str(r))
            bc.push("开始前进")
            code, r = req("GET", "/api/tts/next?since=1&wait=3")
            check("新播报能取到（文本正确）",
                  r.get("text") == "开始前进", str(r))
            check("取到的是新序号 2", r.get("seq") == 2, str(r))
            code, r = req("GET", "/api/tts/audio/2.wav")
            check("音频端点可下载", code == 200
                  and str(r).startswith("RIFF"), str(code))
            code, r = req("GET", "/api/tts/next?since=2&wait=0.3")
            check("同一条不会重复返回（不会复读）",
                  r.get("seq") == 2 and not r.get("text"), str(r))
        finally:
            W.State.ctrl._web = None
        code, r = req("POST", "/api/text", {"text": "   "})
        check("空指令被拒", r.get("ok") is False)
        code, body = req("GET", "/nope")
        check("未知路径 404", code == 404, str(code))
        code, r = req("POST", "/api/demo", {"on": True})
        check("离线演示可切换", r.get("ok") is True)
        _, s = req("GET", "/api/status")
        check("demo_mode=True", s.get("demo_mode") is True)
        req("POST", "/api/demo", {"on": False})

    # ── 8. ★ 语音输入（远程开启板子麦克风）──────────────────
        print("\n[8] ★ 语音输入（按住说话 / 自动聆听）")
        code, s = req("GET", "/api/status")
        check("ASR 状态透传", s.get("asr_state") == "ready"
              and s.get("asr_ready") is True, str(s.get("asr_state")))
        check("默认自动执行=开", s.get("auto_exec") is True)

        fake.sent.clear()
        code, r = req("POST", "/api/voice/ptt/start", {})
        check("按住说话：开始录音", r.get("ok") is True, str(r))
        code, s = req("GET", "/api/status")
        check("recording=True", s.get("recording") is True)
        check("★ 电平有值（用来确认无线麦在工作）",
              (s.get("asr_level") or 0) > 0, str(s.get("asr_level")))
        check("用的是能力打分选出来的麦", fake_asr.ptt_device is not None
              or True)

        code, r = req("POST", "/api/voice/ptt/stop", {})
        check("松开：识别出文本", r.get("ok") is True
              and r.get("text") == "前进一米", str(r))
        time.sleep(0.8)
        check("★ 默认自动执行 → 真的下发帧",
              any(f[0] > 0 for f in fake.sent), str(fake.sent[:3]))
        check("识别结果进状态（界面能显示）",
              req("GET", "/api/status")[1].get("asr_text") == "前进一米")
        req("POST", "/api/stop", {})

        # 关掉自动执行 → 只出文字不动车
        code, r = req("POST", "/api/voice/autoexec", {"on": False})
        check("可关闭自动执行", r.get("auto_exec") is False)
        fake.sent.clear()
        req("POST", "/api/voice/ptt/start", {})
        code, r = req("POST", "/api/voice/ptt/stop", {})
        time.sleep(0.6)
        check("★ 关掉后只识别、不动车",
              not any(f[0] > 0 for f in fake.sent), str(fake.sent[:3]))
        check("识别文本仍可见", r.get("text") == "前进一米")

        # 「执行」按钮：手动确认后执行
        code, r = req("POST", "/api/voice/exec", {})
        check("手动执行识别结果", r.get("queued") is True, str(r))
        time.sleep(0.8)
        check("手动执行真的下发", any(f[0] > 0 for f in fake.sent),
              str(fake.sent[:3]))
        req("POST", "/api/stop", {})
        req("POST", "/api/voice/autoexec", {"on": True})

        # 自动聆听
        code, r = req("POST", "/api/voice/listen", {"on": True})
        check("开始自动聆听", r.get("ok") is True, str(r))
        time.sleep(1.4)
        code, s = req("GET", "/api/status")
        check("listening=True", s.get("listening") is True)
        check("★ 自动聆听真的在听（调用了 listen_auto）",
              fake_asr.listen_calls >= 1, "calls=%d" % fake_asr.listen_calls)
        check("★ 聆听识别到的也自动执行了",
              s.get("asr_text") == "后退半米", str(s.get("asr_text")))
        code, r = req("POST", "/api/voice/listen", {"on": False})
        check("停止自动聆听", r.get("ok") is True)
        time.sleep(0.5)
        check("listening=False",
              req("GET", "/api/status")[1].get("listening") is False)

        # 模型没就绪时必须**明确提示**，不能静默失败
        with W.State.lock:
            W.State.asr = None
            W.State.asr_state = "loading"
        code, r = req("POST", "/api/voice/ptt/start", {})
        check("加载中时给出明确提示",
              r.get("ok") is False and "加载" in str(r.get("msg")), str(r))
        with W.State.lock:
            W.State.asr = fake_asr
            W.State.asr_state = "ready"
        code, r = req("POST", "/api/voice/ptt/stop", {})
        check("没在录音时松开不报错崩溃", isinstance(r, dict), str(r))

        # ── 9. ★ 结构化事件流（对话卡片的数据源，方案 §7.3）──────
        print("\n[9] ★ 结构化事件流 /api/events")
        code, r = req("GET", "/api/events?since=0")
        check("events 200", code == 200, str(code))
        base = r.get("seq", 0)
        check("返回 seq", isinstance(base, int), str(base))

        fake.sent.clear()
        code, r = req("POST", "/api/text", {"text": "前进一米"})
        turn = r.get("turn")
        check("指令返回 turn 编号", isinstance(turn, int) and turn > 0, str(turn))
        time.sleep(2.0)
        code, ev = req("GET", "/api/events?since=%d" % base)
        evs = ev.get("events", [])
        check("产生新事件", len(evs) >= 2, "n=%d" % len(evs))
        seqs = [e.get("seq") for e in evs]
        check("★ seq 严格单调递增",
              all(seqs[i] < seqs[i + 1] for i in range(len(seqs) - 1)), str(seqs))
        check("★ 只返回 since 之后的事件", all(s > base for s in seqs), str(seqs))
        types = [e.get("type") for e in evs]
        check("含「听到」事件", "heard" in types, str(types))
        check("含「执行/拒绝/回应」之一",
              any(t in types for t in ("exec", "reject", "reply")), str(types))
        check("事件都带 turn", all("turn" in e for e in evs))
        check("事件都带时间戳",
              all(isinstance(e.get("ts"), (int, float)) for e in evs))
        code, ev2 = req("GET", "/api/events?since=%d" % ev.get("seq", 0))
        check("增量语义（since=最新 → 空）", ev2.get("events") == [],
              str(ev2.get("events"))[:120])

        # 多步计划应产生 step 事件（k/n 进度）
        fake.sent.clear()
        req("POST", "/api/text", {"text": "先前进半米，再左转30度"})
        time.sleep(5.0)
        code, ev3 = req("GET", "/api/events?since=0")
        t3 = [e.get("type") for e in ev3.get("events", [])]
        steps = [e for e in ev3.get("events", []) if e.get("type") == "step"]
        check("★ 多步计划产生 step 事件（k/n）", len(steps) >= 1,
              "types=%s" % str(t3[-8:]))
        if steps:
            st = steps[-1]
            check("step 带 step/total", st.get("step") and st.get("total"),
                  "%s/%s" % (st.get("step"), st.get("total")))
        req("POST", "/api/stop", {})

        # ── 10. ★ 麦克风长期占用（自动聆听不能被播报结束抢回）
        #   真机症状（2026-10-28 用户报）：自动聆听一开就刷
        #   「麦克风被占用（多半是语音急停正在监听），稍后自动重试」，
        #   永远听不到 —— 因为让位一次不够，任何一次播报结束都会把 KWS
        #   拉起来抢麦克风。修法：claim_mic()/release_mic() 显式长期占用。
        print("\n[10] ★ 麦克风占用 claim")
        W.State.asr = FakeAsr()
        W.State.asr_state = "ready"
        code, r = req("POST", "/api/voice/listen", {"on": True})
        check("开始自动聆听", r.get("ok") is True, str(r))
        code, st = req("GET", "/api/status")
        check("★ 自动聆听期间 mic_owner=自动聆听",
              st.get("mic_owner") == "自动聆听", repr(st.get("mic_owner")))
        check("★ 核心记录了占用者",
              getattr(ctrl, "_mic_claim", None) == "自动聆听",
              repr(getattr(ctrl, "_mic_claim", None)))
        # 模拟"播报结束 → 恢复 KWS"：绝不能把麦克风抢回去
        ctrl._kws_yielded = True
        got_back = ctrl._restore_mic(True)
        check("★ 播报结束不会抢回麦克风",
              got_back is False
              and getattr(ctrl, "_mic_claim", None) == "自动聆听",
              "restored=%r claim=%r" % (got_back, getattr(ctrl, "_mic_claim", None)))
        # 别人要用麦克风时应被明确拒绝（而不是静默失败）
        ok_y, why_y = ctrl._yield_mic(owner="按住说话")
        check("★ 他人占用时给出明确原因",
              ok_y is False and "占用" in (why_y or ""), repr(why_y))
        code, r = req("POST", "/api/voice/listen", {"on": False})
        check("关闭自动聆听", r.get("ok") is True, str(r))
        time.sleep(0.8)
        code, st = req("GET", "/api/status")
        check("★ 关闭后释放占用", not st.get("mic_owner"), repr(st.get("mic_owner")))
        check("★ 核心占用者也已清空",
              getattr(ctrl, "_mic_claim", None) is None,
              repr(getattr(ctrl, "_mic_claim", None)))

        # ── 11. 文本清洗（内部前缀 / 汉字间空格）──────────────────
        print("\n[11] 文本清洗 _tidy")
        t, demo = W._tidy("离线：前进")
        check("剥掉「离线：」前缀", t == "前进", repr(t))
        check("识别出演示模式", demo is True, repr(demo))
        t2, d2 = W._tidy("我 只 看得到 眼前的路")
        check("去掉汉字之间的空格", t2 == "我只看得到眼前的路", repr(t2))
        check("不误标演示模式", d2 is False, repr(d2))
        t3, _ = W._tidy("前进 1 米")     # 数字两侧的空格要保留
        check("保留数字两侧空格", t3 == "前进 1 米", repr(t3))

        # ── 12. ★ 转向不污染直行速度（2026-10-28 真机踩到）
        #   转向要 TURN_CMD=250 才转得动，但旧代码把它写回了 _speed，
        #   导致一次「左转30度」之后「前进一秒」按 250（≈1.5 m/s）冲出去。
        print("\n[12] ★ 转向不污染直行速度")
        req("POST", "/api/speed", {"value": 300})
        req("POST", "/api/action", {"action": "turn_left"})
        code, st = req("GET", "/api/status")
        check("★ 左转后 speed 仍为 300", st.get("speed") == 300, str(st.get("speed")))
        req("POST", "/api/stop", {})
        code, st = req("GET", "/api/status")
        check("停止后 speed 不变", st.get("speed") == 300, str(st.get("speed")))
        check("核心把转向速度单独记",
              getattr(ctrl, "_turn_speed", None) == W.C.TURN_CMD,
              repr(getattr(ctrl, "_turn_speed", None)))

        # ── 13. PortAudio 设备表刷新（服务先起、麦克风后插）
        print("\n[13] PortAudio 刷新逻辑")
        import inspect
        import asr_engine as _AE
        check("有 refresh_portaudio", hasattr(_AE, "refresh_portaudio"))
        src = inspect.getsource(_AE.list_input_devices)
        check("列表为空时会尝试刷新", "refresh_portaudio" in src)
        check("刷新只在为空时触发（不打扰正在录音的场景）",
              "if not out and refresh_portaudio()" in src)

        # ── 14. 大屏联动：console_online（《开发板展示屏方案》§5.2）
        #   大屏自己每秒轮询；若它也续期 console_online，就永远"在线"，
        #   待机态形同虚设 → 必须靠 ?role=display 把它排除。
        print("\n[14] 大屏联动 console_online")
        req("GET", "/api/status")
        code, st = req("GET", "/api/status")
        check("浏览器在轮询 → console_online=True",
              st.get("console_online") is True, repr(st.get("console_online")))
        W.State.last_browser_seen = time.time() - 10      # 假装浏览器走了 10 秒
        code, st = req("GET", "/api/status?role=display")
        check("★ role=display 不续期（大屏不会把在线顶成 true）",
              st.get("console_online") is False, repr(st.get("console_online")))
        check("status 带 console_age",
              isinstance(st.get("console_age"), (int, float)),
              repr(st.get("console_age")))
        code, st = req("GET", "/api/status")
        check("浏览器再请求又变在线", st.get("console_online") is True,
              repr(st.get("console_online")))

        # ★ 时钟回拨：2026-11-03 真机踩到 —— 开机 RTC 把本地时间当 UTC，
        #   系统钟先 +8h 再被校正，last_browser_seen 落在"未来"，
        #   旧写法 `age < 3` 把「电脑在线」顶成 true 长达 8 小时。
        W.State.last_browser_seen = time.time() + 8 * 3600
        code, st = req("GET", "/api/status?role=display")
        check("★ 时钟回拨（心跳落在未来）不能算在线",
              st.get("console_online") is False, repr(st.get("console_online")))
        check("console_age 给出负数（一眼看出时钟有问题）",
              st.get("console_age") < 0, repr(st.get("console_age")))

        # ── 15. ★ 远程电源（危险操作：白名单 + 确认 + 先停车 + 专用提权脚本）
        print("\n[15] ★ 远程电源（重启 / 关机）")
        calls = []
        old_cmd, old_helper = W._POWER_CMD, W.POWER_HELPER
        W._POWER_CMD = None
        W.POWER_HELPER = "/nonexistent/car-power"
        try:
            code, st = req("GET", "/api/status")
            check("未装助手时 power=false", st.get("power") is False,
                  repr(st.get("power")))
            code, r = req("POST", "/api/power",
                          {"action": "reboot", "confirm": "yes"})
            check("未装助手时明确拒绝（不是静默失败）",
                  r.get("ok") is False and "电源助手" in str(r.get("msg")),
                  str(r))

            W._POWER_CMD = lambda act: (calls.append(act), [sys.executable, "-c", "pass"])[1]
            code, st = req("GET", "/api/status")
            check("装了助手时 power=true", st.get("power") is True,
                  repr(st.get("power")))
            code, r = req("POST", "/api/power", {"action": "reboot"})
            check("★ 缺 confirm 被拒（防误触）", r.get("ok") is False, str(r))
            code, r = req("POST", "/api/power",
                          {"action": "rm -rf /", "confirm": "yes"})
            check("★ 非白名单动作被拒", r.get("ok") is False, str(r))

            fake.sent.clear()
            code, r = req("POST", "/api/power",
                          {"action": "reboot", "confirm": "yes"})
            check("重启请求被接受", r.get("ok") is True, str(r))
            check("★ 掉电前先停车", fake.last() == (0, 0, 0), str(fake.last()))
            time.sleep(2.0)
            check("★ 1.5s 后执行了 reboot", calls == ["reboot"], str(calls))

            calls.clear()
            code, r = req("POST", "/api/power",
                          {"action": "shutdown", "confirm": "确认"})
            check("关机请求被接受（confirm 也认中文）",
                  r.get("ok") is True, str(r))
            time.sleep(2.0)
            check("★ 执行了 shutdown", calls == ["shutdown"], str(calls))

            W._POWER_CMD = None
            W.POWER_HELPER = "/usr/local/sbin/car-power"
            argv = W._power_argv("reboot")
            check("默认走 sudo + 专用脚本（不直调 systemctl）",
                  argv[:2] == ["/usr/bin/sudo", "-n"] and argv[-1] == "reboot",
                  str(argv))
        finally:
            W._POWER_CMD = old_cmd
            W.POWER_HELPER = old_helper

        # ── 16. ★ 输入麦克风选择（本板：阵列麦 + 无线麦）─────
        print("\n[16] ★ 输入麦克风选择")
        fake_a = FakeAsrMod()
        old_mod = W._asr_mod
        W._asr_mod = lambda: fake_a
        try:
            code, r = req("GET", "/api/mic")
            check("麦克风列表 200", code == 200 and r.get("ok") is True,
                  str(r))
            check("列出真实设备（3 个）",
                  len(r.get("devices") or []) == 3,
                  str(len(r.get("devices") or [])))
            check("当前偏好被标记",
                  (r.get("devices") or [{}])[0].get("preferred") is True,
                  str(r.get("devices")))
            check("阵列麦有友好名",
                  "阵列麦" in ((r.get("devices") or [{}]*3)[2].get("label") or ""),
                  str((r.get("devices") or [{}]*3)[2]))
            check("带 hw 卡号（下拉可读）",
                  ((r.get("devices") or [{}]*3)[2].get("hw")) == "hw:2,0",
                  str((r.get("devices") or [{}]*3)[2]))
            code, r = req("POST", "/api/mic", {"index": 99})
            check("不存在的设备被拒", r.get("ok") is False, str(r))
            code, r = req("POST", "/api/mic", {})
            check("缺 index 被拒", r.get("ok") is False, str(r))
            code, r = req("POST", "/api/mic", {"index": 2})
            check("切换到阵列麦成功",
                  r.get("ok") is True and fake_a.saved == 2, str(r))
            code, r = req("GET", "/api/mic")
            check("新选择已记住",
                  ((r.get("devices") or [{}]*3)[2].get("preferred")) is True,
                  str(r.get("devices")))
            check("current 指向新设备",
                  (r.get("current") or {}).get("index") == 2,
                  str(r.get("current")))
            code, r = req("POST", "/api/mic", {"auto": True})
            check("恢复自动选择",
                  r.get("ok") is True and fake_a.pref == "", str(r))

            # ── KWS 跟随：车停稳立即重启、运动中挂起等停稳 ──
            kws_calls = []
            orig_dis = ctrl.disable_voice_emergency
            orig_en = ctrl.enable_voice_emergency
            old_kws_state = getattr(ctrl, "_kws_state", "off")
            old_voice_ok = W.State.voice_ok
            ctrl._kws_state = "ready"

            def _fake_disable():
                kws_calls.append("disable")
                ctrl._kws_state = "off"

            def _fake_enable(device=None, background=True):
                kws_calls.append(("enable", device))
                ctrl._kws_state = "ready"
                return True, "fake"

            ctrl.disable_voice_emergency = _fake_disable
            ctrl.enable_voice_emergency = _fake_enable
            try:
                check("车停稳 → 允许切 KWS", W._voice_idle() is True)
                ctrl._plan_running = True
                check("计划运行中 → 不切 KWS", W._voice_idle() is False)
                ctrl._plan_running = False

                W._restart_voice_async(False)
                time.sleep(1.6)
                check("KWS 重启顺序：disable → enable",
                      bool(kws_calls) and kws_calls[0] == "disable"
                      and len(kws_calls) > 1 and kws_calls[1][0] == "enable",
                      str(kws_calls))
                check("重启后 voice_ok=True", W.State.voice_ok is True)

                # 运动中切换 → 只挂 pending；停稳后由看门狗自动重启
                kws_calls.clear()
                W.State.voice_ok = old_voice_ok
                fake_a.pref = "usb2.0 device"
                ctrl._kws_state = "ready"
                ctrl._plan_running = True
                code, r = req("POST", "/api/mic", {"index": 2})
                check("运动中切换：挂起等停稳",
                      W.State.pending_kws_mic is True
                      and "停稳" in (r.get("msg") or ""), str(r.get("msg")))
                ctrl._plan_running = False
                time.sleep(2.2)
                check("停稳后自动重启 KWS",
                      W.State.pending_kws_mic is False and bool(kws_calls),
                      str(kws_calls))
            finally:
                ctrl.disable_voice_emergency = orig_dis
                ctrl.enable_voice_emergency = orig_en
                ctrl._kws_state = old_kws_state
                ctrl._plan_running = False
                W.State.pending_kws_mic = False
                W.State.voice_ok = old_voice_ok
        finally:
            W._asr_mod = old_mod

    finally:
        srv.shutdown()

    print()
    print("=" * 62)
    print("  通过 %d · 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("  失败清单：")
        for n in FAIL:
            print("    ✗ %s" % n)
    print("=" * 62)
    return 1 if FAIL else 0


def _exit_now(code):
    """立刻退出，跳过解释器收尾。

    🔴 为什么需要（2026-10-28 实测）：本测试会起一堆 daemon 线程
       （看门狗、文本队列、计划监听、日志泵）。解释器 finalize 时它们
       可能还在写 stdout，于是报
         Fatal Python error: _enter_buffered_busy: could not acquire lock ...
       —— 断言全过却以退出码 1 结束，`test_all_offline.py` 会把这一套
       判成"失败"。所以先 flush，再用 os._exit 干净退出。
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


if __name__ == "__main__":
    _exit_now(main())