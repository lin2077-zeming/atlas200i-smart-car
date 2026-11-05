#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
car_web.py —— **无界面远程 Web 控制台**（在笔记本浏览器里控制小车）

为什么要有它
------------
板子自带的 PySide6 界面跑在车上那块小屏幕上，还得接键鼠，现场很别扭。
这个脚本把 `CarController` 直接跑在**后台**（不依赖 Qt / 显示器），
再用一个极简网页把「状态 + 控制」暴露给同一局域网里的浏览器。

```
笔记本浏览器  ──HTTP──►  板子:8090（本脚本）  ──串口──►  底盘
                              └─ 复用已有「网络音箱」:8099 → 浏览器当车的喇叭
```

用法
----
    bash run_web.sh                       # 默认 8090 端口，自动连串口
    bash run_web.sh --port 8090 --serial /dev/ttyCAR
    bash run_web.sh --no-voice            # 不占用麦克风（不开语音急停）
    bash run_web.sh --asr                 # 额外加载语音识别 → 支持「小车车」唤醒
    bash run_web.sh --demo                # 离线演示：不发串口，只看界面

打开： http://<板子IP>:8090/

设计要点（改代码前必读）
------------------------
1. **必须与 `CarController` 同进程**：串口是独占资源，另起进程会抢串口。
2. **按住走、松开停**：网页的方向键按下发 `duration=None`（持续运动），
   松开发 `/api/stop`。一旦笔记本断网/关页面，`/api/stop` 就到不了 ——
   所以有 3 秒 **心跳看门狗**：只有"网页发起的持续运动"才会被它兜底停车
   （定时运动和多步计划有自己的定时器，不受影响）。
3. **急停优先**：`/api/estop` 直接走 `trigger_emergency()`（停车 → 锁定 → 播报），
   不经过任何队列，页面上永远是最大那个按钮。
4. **文本入口是给"电脑端语音识别"预留的**：以后在电脑上把语音转成文字，
   直接 POST `/api/text` 即可，板子这边不用改。
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import car_gui_core as C                                       # noqa: E402
from car_gui_core import mmps_for_display                      # noqa: E402

UI_FILE = os.path.join(HERE, "web_ui.html")

#: 网页允许下发的动作（**故意不含 strafe_left/right** —— 本车实测不支持平移）
WEB_ACTIONS = ("forward", "backward", "turn_left", "turn_right", "stop")

#: 心跳看门狗：网页发起的持续运动，超过这个秒数没心跳就自动停车
HOLD_TIMEOUT = 3.0

#: 日志环形缓冲保留条数
LOG_KEEP = 400

#: 单条文本指令最大长度
TEXT_MAX = 200

#: 远程电源：只允许这两个动作（高级设置里的「重启 / 关机」）
POWER_ACTIONS = {"reboot": "重启", "shutdown": "关机"}

#: 提权入口：由 `setup_power.sh` 安装的**唯一**脚本，sudoers 只放行它。
#: 不用 `sudo systemctl ...` 直调，避免把整个 systemctl 暴露给服务账号。
POWER_HELPER = "/usr/local/sbin/car-power"

#: 测试注入：callable(action) -> argv；None 时走 sudo + POWER_HELPER
_POWER_CMD = None


def _power_available():
    """板子有没有装电源助手（没装就让界面隐藏这几个按钮）。"""
    return callable(_POWER_CMD) or os.path.exists(POWER_HELPER)


def _power_argv(action):
    if callable(_POWER_CMD):
        return _POWER_CMD(action)
    return ["/usr/bin/sudo", "-n", POWER_HELPER, action]


def _schedule_power(action, ctrl):
    """1.5 秒后重启/关机：**先停车、先回包**，再让系统掉电。

    延迟执行的原因：HTTP 回包要先发给浏览器，否则页面只会看到
    "请求失败"，用户以为没生效；1.5 s 足够回包 + 前端 toast。
    """
    label = POWER_ACTIONS[action]
    try:
        ctrl.stop()                       # ★ 先停车，不能带着运动指令掉电
    except Exception:
        pass
    ctrl.log("🛑 网页请求%s开发板：1.5 秒后执行…（页面会断开）" % label)

    def _run():
        try:
            argv = _power_argv(action)
            ctrl.log("🛑 执行：%s" % " ".join(argv))
            subprocess.Popen(argv, close_fds=True, start_new_session=True,
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as e:
            ctrl.log("❌ %s失败：%r" % (label, e))

    threading.Timer(1.5, _run).start()


class State(object):
    """全局共享状态（HTTP 线程 / 看门狗 / 日志泵 都会碰）。"""

    ctrl = None
    ui = ""
    boot = time.time()
    lock = threading.Lock()
    #: 心跳时间戳（网页每秒 /api/status 即续期）
    hb = time.time()
    #: 是否处于"网页发起的持续运动"
    hold = False
    hold_action = None
    #: 日志环形缓冲 [(n, line)]
    logs = []
    log_seq = 0
    #: 文本指令队列（串行执行，避免并发下发）
    q = queue.Queue()
    #: 语音急停 / 唤醒 是否可用
    voice_ok = False
    wake_ok = False
    wake_note = "未加载语音识别（启动时加 --asr 可开启唤醒）"
    #: 用户切了麦克风、但车正在动：等停稳后由看门狗自动重启语音急停
    pending_kws_mic = False
    # ── 语音输入（ASR）────────────────────────────────────────────
    #: AsrEngine 实例（None = 还没加载）
    asr = None
    #: off / loading / ready / failed
    asr_state = "off"
    asr_msg = ""
    #: 最近一次识别结果
    asr_text = ""
    asr_cost = 0.0
    asr_seq = 0
    #: 是否正在自动聆听 / 正在录音（按住说话）
    listening = False
    recording = False
    #: 识别后是否自动执行指令（**默认开**，用户可在界面关掉）
    auto_exec = True
    #: 按住说话时向 KWS"借"过来的麦克风，松手要还
    mic_yielded = False
    # ── 结构化事件流（对话卡片用；2026-10-28 按改版方案 §7.2 加）─────
    #: 环形缓冲，元素见 _emit()；保留 EV_KEEP 条
    events = []
    ev_seq = 0
    #: 当前"一轮交互"的编号：一次用户输入 = 一个 turn，卡片按它聚合
    turn = 0
    #: ★ 最近一次**浏览器控制台**（非大屏）请求的时间戳。
    #:   大屏展示端轮询 `/api/status?role=display` 时**不**更新它 ——
    #:   否则大屏自己就把 console_online 永远顶成 true，待机态形同虚设
    #:   （见《开发板展示屏方案》§5.2）。
    last_browser_seen = 0.0


#: 事件环形缓冲保留条数（网页按 since 增量取，200 条够回看）
EV_KEEP = 240

#: 这些字样出现在返回消息里 => 判定为"被拒绝"，而不是闲聊回应
_REJECT_HINTS = ("急停锁定", "未连接", "不支持", "已有计划", "未识别",
                 "没听懂", "计划为空")


def _emit(etype, turn=None, **fields):
    """记一条结构化事件（对话卡片的数据源）。

    ⚠️ 与 `/api/logs`（纯文本，给"高级"面板）**并存**，不是替代：
       日志给人看，事件给界面拼卡片。
    """
    with State.lock:
        State.ev_seq += 1
        ev = {"seq": State.ev_seq, "ts": round(time.time(), 3),
              "type": etype,
              "turn": State.turn if turn is None else turn}
        ev.update(fields)
        State.events.append(ev)
        if len(State.events) > EV_KEEP:
            del State.events[:len(State.events) - EV_KEEP]
    return ev


def _new_turn():
    """开一轮新交互（一次用户输入 = 一张卡片）。"""
    with State.lock:
        State.turn += 1
        return State.turn


_CJK_GAP = re.compile("(?<=[\u4e00-\u9fff])\\s+(?=[\u4e00-\u9fff])")
_DEMO_PREFIXES = ("离线：", "离线:")


def _tidy(text):
    """把给**人看**的文本收拾干净。

    · 小模型偶尔会在汉字之间插入空格（"我 只 看得到眼前的路"），
      界面上看着像断字/错字 —— 去掉汉字之间的空白。
    · `run_text_command()` 在演示模式下会给消息加「离线：」前缀，
      那是内部标记，不该露给用户（界面改成「离线演示」徽标）。
    返回 (收拾后的文本, 是否演示模式)。
    """
    t = str(text or "").strip()
    demo = False
    for p in _DEMO_PREFIXES:
        if t.startswith(p):
            demo = True
            t = t[len(p):].strip()
            break
    return _CJK_GAP.sub("", t), demo


def _classify_result(turn, ok, msg):
    """把 `run_text_command` 的结果归类成事件：执行 / 拒绝 / 闲聊回应。"""
    text, demo = _tidy(msg)
    if ok:
        _emit("exec", turn, label=text, ok=True, demo=demo)
        return
    if any(h in text for h in _REJECT_HINTS):
        _emit("reject", turn, reason=text, demo=demo)
    else:
        # 闲聊：msg 就是它回的那句话
        _emit("reply", turn, text=text)


# ══════════════════════════════════════════════════════════════════════
#  后台线程
# ══════════════════════════════════════════════════════════════════════
def _log_pump():
    """把控制器日志搬进环形缓冲（网页按序号增量取）。"""
    while True:
        try:
            items = State.ctrl.drain_logs()
        except Exception:
            items = []
        if items:
            with State.lock:
                for it in items:
                    State.log_seq += 1
                    State.logs.append((State.log_seq, it))
                if len(State.logs) > LOG_KEEP:
                    del State.logs[:len(State.logs) - LOG_KEEP]
        time.sleep(0.25)


def _watchdog():
    """心跳看门狗：断网/关页面时，兜底停住"网页发起的持续运动"。

    ⚠️ 只兜底 `hold`（网页按住走的那些），**不去动**：
       · 定时运动（`move(..., duration)` 自带 Timer 停车）
       · 多步计划（`_run_plan` 自己逐步定时，且会被急停打断）
    否则会误杀用户正常发起的指令。
    """
    while True:
        time.sleep(0.4)
        with State.lock:
            hold, act, age = State.hold, State.hold_action, time.time() - State.hb
        if hold and age > HOLD_TIMEOUT:
            with State.lock:
                State.hold = False
                State.hold_action = None
            try:
                State.ctrl.stop()
                State.ctrl.log("⏱ 网页心跳超时 %.1fs → 已自动停车（防断网跑飞）" % age)
            except Exception as e:
                State.ctrl.log("看门狗停车失败：%r" % (e,))

        # 切麦克风时车还在动 → 等停稳后再重启语音急停
        # （避免"车还在跑、语音急停却空了 8~10 秒"）
        with State.lock:
            pend = State.pending_kws_mic
        if pend and _voice_idle():
            with State.lock:
                if State.pending_kws_mic:
                    State.pending_kws_mic = False
                    listening = State.listening
                else:
                    listening = None
            if listening is not None:
                State.ctrl.log("🎙 小车已停稳 → 开始按新麦克风重启语音急停")
                _restart_voice_async(listening)


def _text_worker():
    """串行执行文本指令。

    `/api/text` 立刻返回「已提交」，实际执行放这里 ——
    否则 LLM 纠错可能要 3~10 秒，HTTP 早就超时了。
    """
    while True:
        item = State.q.get()
        if item is None:
            return
        # 队列里是 (text, turn)；兼容只塞了字符串的老调用
        if isinstance(item, tuple):
            text, turn = item
        else:
            text, turn = item, None
        try:
            ok, msg = State.ctrl.run_text_command(text)
            _classify_result(turn, ok, msg)
        except Exception as e:
            State.ctrl.log("文本指令执行异常：%r" % (e,))
            _emit("reject", turn, reason="执行异常")
        finally:
            State.q.task_done()


def _plan_watcher():
    """盯着多步计划进度，变化时记一条 `step` 事件（网页卡片显示 k/n）。

    为什么用"盯"而不是在 core 里埋事件回调：
      · core 不该知道 Web 层的事件模型（保持零依赖）；
      · 直接读 `_plan_step/_plan_total/_plan_label`，开销极小，
        而且规则层与 LLM 层两条入口都自动覆盖到。
    """
    ctrl = State.ctrl
    last = ()
    was_running = False
    while True:
        time.sleep(0.2)
        try:
            running = bool(getattr(ctrl, "_plan_running", False))
            step = int(getattr(ctrl, "_plan_step", 0) or 0)
            total = int(getattr(ctrl, "_plan_total", 0) or 0)
            label = getattr(ctrl, "_plan_label", "") or ""
        except Exception:
            continue
        key = (step, total, label)
        if running and step and key != last:
            _emit("step", step=step, total=total, label=label)
            last = key
        if was_running and not running:
            _emit("plan_done")
            last = ()
        was_running = running


def _serial_candidates():
    """当前存在的串口设备（用于"插上再连"，避免没插时反复报错刷屏）。"""
    out = []
    for p in ("/dev/ttyCAR", "/dev/ttyACM0", "/dev/ttyACM1",
              "/dev/ttyUSB0", "/dev/ttyUSB1"):
        if os.path.exists(p):
            out.append(p)
    return out


def _autoconnect(ctrl, port, interval=3.0):
    """自动连串口：**没插就静默等**，插上就自动连、拔了会自动重连。

    ⚠️ 不能每 3 秒无脑调 `ctrl.connect()` —— 那会在串口不存在时
       每 3 秒往日志里写一条失败（日志页会被刷爆）。
       所以先看设备在不在，只在"状态变化"时提示一次。
    """
    note = ""
    while True:
        try:
            if ctrl.connected:
                time.sleep(2.0)
                continue
            if port:
                avail = os.path.exists(port)
            else:
                avail = bool(_serial_candidates())
            if not avail:
                if note != "wait":
                    ctrl.log("等待串口插入…（插好后会自动连接）")
                    note = "wait"
                time.sleep(interval)
                continue
            note = "try"
            ok, msg = ctrl.connect(port=port)
            if ok:
                note = ""
            else:
                time.sleep(interval)
        except Exception as e:
            ctrl.log("自动连接异常：%r" % (e,))
            time.sleep(interval)


# ══════════════════════════════════════════════════════════════════════
#  语音输入（ASR）：懒加载 + 按住说话 + 自动聆听
# ══════════════════════════════════════════════════════════════════════
def _asr_device():
    """挑麦克风：用 asr_engine 的"能力打分"选择（本板的'USB 音箱'会被排除）。"""
    try:
        import asr_engine as A
        return A.guess_input_device()
    except Exception:
        return None


def _asr_level():
    """录音/聆听时的音量 0~1（界面上的电平条 —— **用来确认无线麦到底有没有在工作**）。"""
    try:
        eng = State.asr
        if eng is None or not getattr(eng, "recording", False):
            return 0.0
        return round(float(getattr(eng, "level", 0.0) or 0.0), 3)
    except Exception:
        return 0.0


def _asr_mod():
    """延迟 import asr_engine（测试里可以整体替换成假模块）。"""
    import asr_engine as A
    return A


#: 已知设备的友好名（只是显示用；匹配的是 `device_identity` 归一化后的串）
_MIC_HINTS = (
    ("dov", "阵列麦 · QXMIC"),
    ("spacetouch", "无线麦"),
)


def _mic_hint(ident):
    low = (ident or "").lower()
    for key, label in _MIC_HINTS:
        if key in low:
            return label
    return ""


def _alsa_card_descs():
    """读 /proc/asound/cards → {卡号: 厂商/型号描述}（失败返回空 dict）。

    sounddevice 的设备名有时只有 "USB Audio: - (hw:1,0)"，看不出是哪只麦；
    /proc/asound/cards 的下一行有厂商串（如 "SPACETOUCH USB Audio"），
    拿它给下拉框补个可读名。非 Linux / 读不到时静默跳过。
    """
    out = {}
    try:
        with open("/proc/asound/cards", encoding="utf-8",
                  errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return out
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*(\d+)\s*\[", ln)
        if not m:
            continue
        n = int(m.group(1))
        desc = ""
        if i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt:
                desc = nxt.split(" at ")[0].strip()
        out[n] = desc
    return out


def _mic_snapshot():
    """列出真实录音设备 + 当前选择（给网页「输入麦克风」下拉框）。

    返回：
        {"ok": True, "devices": [{index,name,label,hint,identity,
                                   samplerate,channels,preferred}],
         "preferred": 身份串, "current": 当前会自动选中的设备 dict,
         "auto": 是否自动选择}
    """
    try:
        A = _asr_mod()
    except Exception as e:
        return {"ok": False, "msg": "缺少 asr_engine：%r" % (e,),
                "devices": []}
    try:
        devs = A.real_input_devices()
    except Exception as e:
        return {"ok": False, "msg": "枚举麦克风失败：%r" % (e,),
                "devices": []}
    pref = ""
    try:
        pref = A._load_preferred_input()
    except Exception:
        pref = ""
    cards = _alsa_card_descs()
    out = []
    for d in devs:
        name = d.get("name") or "?"
        ident = ""
        try:
            ident = A.device_identity(name)
        except Exception:
            pass
        hint = _mic_hint(ident)
        m = re.search(r"\(hw:(\d+),", name)
        card = cards.get(int(m.group(1)), "") if m else ""
        hw = ("hw:%s,0" % m.group(1)) if m else ""
        out.append({
            "index": int(d.get("index", -1)),
            "name": name,
            "identity": ident,
            "label": ("%s · %s" % (hint, name)) if hint else name,
            "hint": hint,
            "card": card,
            "hw": hw,
            "samplerate": int(d.get("samplerate") or 0),
            "channels": int(d.get("channels") or 0),
            "preferred": bool(pref) and ident == pref,
        })
    cur = None
    try:
        idx = A.guess_input_device()
        cur = next((x for x in out if x["index"] == idx), None)
    except Exception:
        pass
    return {"ok": True, "devices": out, "preferred": pref,
            "current": cur, "auto": not pref}


def _restart_listening_async():
    """自动聆听中切换了麦克风 → 停掉再重开，让新设备立即生效。

    ⚠️ 只能放后台：`_listen(False)` 要等采集线程退出、ALSA 释放，
       再 `_listen(True)` 重新 claim 麦克风 —— 同步做 HTTP 早就超时了。
    """
    def _run():
        try:
            Handler._listen(False)
            time.sleep(0.8)          # 等 ALSA 真正释放采集端点
            r = Handler._listen(True)
            State.ctrl.log("🎙 已按新麦克风重启自动聆听：%s"
                           % (r.get("msg") or ""))
        except Exception as e:
            State.ctrl.log("🎙 自动聆听重启失败：%r" % (e,))

    threading.Thread(target=_run, daemon=True,
                     name="web-mic-restart").start()


def _voice_idle():
    """当前是否适合重启语音急停（车没动、没有多步计划在跑）。"""
    ctrl = State.ctrl
    try:
        action = (ctrl.status() or {}).get("action")
    except Exception:
        action = None
    if action not in (None, "", "stop"):
        return False
    if getattr(ctrl, "_plan_running", False):
        return False
    with State.lock:
        hold = State.hold
    return not hold


def _restart_voice_async(was_listening=False):
    """按新麦克风重启 KWS（语音急停），并恢复原来的自动聆听状态。

    顺序（不能反）：
      ① 自动聆听若在跑 → 先停掉，把麦克风还给 KWS；
      ② 停旧 KWS → 等 ALSA 真正释放；
      ③ 用**新偏好**重新起 KWS（后台加载模型，约 6~9 秒）——
         这段时间语音急停不可用，所以只在车停稳时才会走到这里；
      ④ 等 KWS 就绪后，再恢复自动聆听（它会按既有逻辑向 KWS 借麦克风）。

    ⚠️ 只在后台跑：HTTP 请求要立刻回包，不能等 KWS 模型加载完。
    """
    def _run():
        ctrl = State.ctrl
        try:
            if was_listening:
                Handler._listen(False)
                time.sleep(0.8)             # 等采集端点真正释放

            ctrl.log("⚠ 语音急停正在切换麦克风（约 8~10 秒不可用）…")
            ctrl.disable_voice_emergency()
            time.sleep(0.6)

            idx = _asr_device()             # 读的是刚保存的新偏好
            ctrl.enable_voice_emergency(device=idx, background=True)

            t0 = time.time()
            while time.time() - t0 < 25.0:
                if getattr(ctrl, "_kws_state", "") in ("ready", "failed"):
                    break
                time.sleep(0.3)
            ok_kws = getattr(ctrl, "_kws_state", "") == "ready"
            State.voice_ok = ok_kws
            ctrl.log("🎙 语音急停已按新麦克风重启：%s（设备 %s）"
                     % ("成功" if ok_kws else "失败",
                        idx if idx is not None else "自动"))
        except Exception as e:
            ctrl.log("🎙 语音急停按新麦克风重启异常：%r" % (e,))
        finally:
            if was_listening:
                time.sleep(0.4)
                try:
                    r = Handler._listen(True)
                    ctrl.log("🎙 自动聆听已恢复：%s" % (r.get("msg") or ""))
                except Exception as e:
                    ctrl.log("🎙 自动聆听恢复失败：%r" % (e,))

    threading.Thread(target=_run, daemon=True,
                     name="web-mic-kws-restart").start()


def _asr_load_worker():
    """后台加载 ASR 模型（228MB SenseVoice，约 10~30 秒）。

    为什么**懒加载**而不是启动就加载：
      板子启动时若把 ASR/KWS/TTS 全拉满，内存和启动时间都吃不消；
      而"这轮要不要用语音"是用户在网页上临时决定的 ——
      界面上点一下「加载语音识别」即可，加载进度会实时显示。
    """
    with State.lock:
        if State.asr_state == "loading":
            return
        State.asr_state = "loading"
        State.asr_msg = "正在加载语音模型（228MB，约 10~30 秒）…"
    ctrl = State.ctrl
    try:
        import asr_engine as A
        eng = A.AsrEngine(log=lambda s: ctrl.log("[ASR] %s" % s))
        md = A.guess_model_dir()
        ok, msg = (eng.load(md) if md
                   else (False, "找不到语音模型目录（先跑 download_asr_model.sh）"))
        with State.lock:
            if ok:
                State.asr = eng
                State.asr_state = "ready"
                State.asr_msg = "语音识别就绪"
            else:
                State.asr_state = "failed"
                State.asr_msg = msg
        ctrl.log("🎤 语音识别：%s" % ("就绪" if ok else "加载失败：%s" % msg))
        if ok:
            # 有 ASR 之后「小车车」唤醒才有意义
            try:
                State.ctrl.set_asr_engine(eng)
                State.wake_ok = True
                State.wake_note = "ASR 已就绪"
            except Exception:
                pass
    except Exception as e:
        with State.lock:
            State.asr_state = "failed"
            State.asr_msg = "加载异常：%r" % (e,)


def _ensure_asr():
    """确保 ASR 可用；未加载就**顺手触发加载**，并告诉调用方等一下。"""
    with State.lock:
        st = State.asr_state
        ready = bool(State.asr is not None and getattr(State.asr, "ready", False))
    if ready:
        return True, ""
    if st != "loading":
        threading.Thread(target=_asr_load_worker, daemon=True,
                         name="web-asr-load").start()
    return False, "语音模型正在加载（约 10~30 秒），请稍候再试"


def _set_recognized(text, cost=0.0):
    """识别出一句话后的统一处理：记下来 → 记事件 → 可选地自动执行。"""
    txt = (text or "").strip()
    with State.lock:
        State.asr_text = txt
        State.asr_cost = cost
        State.asr_seq += 1
        auto = State.auto_exec
    turn = _new_turn()
    _emit("heard", turn, text=txt, src="voice")
    State.ctrl.log("🎤 识别：%s" % (txt or "（空）"))
    if not txt:
        _emit("reject", turn, reason="没听清")
        return
    if not auto:
        # 用户关了"自动执行"：只把识别结果显示出来，等他自己点执行
        _emit("reply", turn, text=txt, pending=True)
        return
    try:
        ok, msg = State.ctrl.run_text_command(txt)
        _classify_result(turn, ok, msg)
    except Exception as e:
        State.ctrl.log("识别结果执行异常：%r" % (e,))
        _emit("reject", turn, reason="执行异常")


def _listen_worker():
    """自动聆听主循环（VAD 断句）：说一句 → 识别 → 继续听。

    ⚠️ 退出时**必须** `release_mic()`：这个循环可能因为 ASR 加载失败、
       用户关掉、异常等原因提前结束，若不释放，语音急停会一直处于让位状态
       （表现为"语音急停明明开着却听不到"）。
    """
    ctrl = State.ctrl
    dev = _asr_device()
    try:
        while True:
            with State.lock:
                if not State.listening:
                    return
                eng = State.asr
            if eng is None or not getattr(eng, "ready", False):
                with State.lock:
                    State.listening = False
                return
            try:
                # 播报没结束就别开麦（本板播放/采集是同一个 subdevice，硬件互斥）
                # ★ 2026-11-05 提速：只等"声卡静下来"，**不等合成队列**
                #   （合成只吃 CPU、不碰音频设备；等它会白搭 ~1.1s/句）。
                #   轮询 200ms → 50ms，播报一停最多 50ms 就能开麦。
                if not ctrl.playback_idle():
                    time.sleep(0.05)
                    continue
                ok, res = eng.listen_auto(device=dev)
            except Exception as e:
                ok, res = False, "异常：%s" % e
            with State.lock:
                still = State.listening
            if not still:
                return
            if ok and res:
                _set_recognized(res)
            else:
                if res:
                    ctrl.log("🎤 聆听：%s" % res)
                # ★ 失败必须**退避**：紧密重试会让 ALSA 反复失败，
                #   最终把 PortAudio 断言 abort 掉（try/except 抓不住）。
                #   板子 GUI 的自动聆听曾因此闪退，这里照抄它的退避做法。
                # ★ 2026-11-05：0.8s → 0.3s（播报刚结束时的"忙"是瞬时的）
                time.sleep(0.3)
    finally:
        # 无论怎么退出，都要把麦克风还回去（安全第一）
        try:
            ctrl.release_mic("自动聆听")
        except Exception:
            pass
        try:
            with State.lock:
                was = State.listening
                State.listening = False
            if was:
                ctrl.log("🎤 自动聆听已结束（麦克风已还给语音急停）")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
#  HTTP
# ══════════════════════════════════════════════════════════════════════
def _ensure_web_speaker(ctrl, port, tries=60):
    """保证网络音箱（浏览器当喇叭）一定起来。

    🔴 为什么 Web 端必须强制它（2026-10-28 真机踩到）：
       本板有一个**只能录音的 USB 声卡**（in=1/out=0）；一旦进程有权限
       （把 HwHiAiUser 加进 audio 组之后），`output_probe()` 看到它就会
       误判成"有喇叭" ⇒ **网络音箱不再启动** ⇒ 浏览器里一句都听不到
       （而 aplay 又必然失败）。远程控制台必须把声音送到**浏览器**，
       所以这里等 TTS 加载好，若 `_web` 仍为空就自己补起一个网络音箱。

    ⚠️ 这一步曾经**只写了调用、漏了函数体** ⇒ 抛 NameError 被 except 吞掉，
       表现为"TTS 就绪但浏览器永远没声音"，日志里只有一句 TTS 初始化失败。
       所以：函数体必须在，且失败要说清楚。
    """
    for _ in range(tries):
        time.sleep(0.5)
        if getattr(ctrl, "_web", None) is not None:
            return True
        eng = getattr(ctrl, "_tts", None)
        if eng is None:
            continue                      # TTS 还在后台加载
        try:
            import tts_serve as _ts
            srv, bc, err = _ts.start_background(eng, port=int(port), log=ctrl.log)
            if err:
                ctrl.log("❌ 补起网络音箱失败：%s" % err)
                return False
            ctrl._web = (srv, bc, int(port))
            ctrl.log("🔊 已补起网络音箱（浏览器当喇叭）：http://%s:%d/listen"
                     % (_ts.local_ip(), int(port)))
            return True
        except Exception as e:
            ctrl.log("❌ 补起网络音箱异常：%r" % (e,))
            return False
    ctrl.log("⚠ 等 TTS 超时，网络音箱没起来（浏览器将听不到声音）")
    return False


class QuietServer(ThreadingHTTPServer):
    """静音版 HTTP 服务：吞掉"客户端断开"这类噪音堆栈。

    浏览器/手机切页、刷新、关标签时非常常见
    `ConnectionResetError: [Errno 104] Connection reset by peer`，
    而 socketserver 默认会把**整个 traceback** 打到 stderr ——
    日志被刷得没法看（真机上实测每分钟好几条）。
    这些都是正常的网络行为，不该当错误报。
    """

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys as _sys
        et = _sys.exc_info()[0]
        if et in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        return ThreadingHTTPServer.handle_error(self, request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "car-web/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        # 轮询很频繁，别刷屏；只在出问题时才需要看
        pass

    # ── 回包 ───────────────────────────────────────────────────────
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

    def _body(self):
        """读请求体；容忍 sendBeacon 的 text/plain（不是 JSON）。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(min(n, 65536))
        except Exception:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return {}

    def _bump(self):
        """刷新心跳（任何一次网页请求都算活着）。"""
        with State.lock:
            State.hb = time.time()

    # ── GET ────────────────────────────────────────────────────────
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        self._bump()

        if p in ("/", "/index.html"):
            return self._send(200, State.ui, "text/html; charset=utf-8")
        if p == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if p == "/api/status":
            # 大屏展示端带 ?role=display → **不续期** console_online，
            # 否则它自己轮询就把"控制台在线"永远顶成 true（方案 §5.2）。
            role = (q.get("role") or [""])[0]
            if role != "display":
                with State.lock:
                    State.last_browser_seen = time.time()
            return self._json(self._status())
        if p == "/api/logs":
            return self._json(self._logs(q))
        if p == "/api/events":
            return self._json(self._events(q))
        # ── 输入麦克风（本板：阵列麦 + 无线麦，网页可切换）──
        if p == "/api/mic":
            return self._json(_mic_snapshot())
        # ── 浏览器当喇叭（与 8099 同一份广播对象，同源免跨域）──
        if p == "/api/tts/seq":
            bc = self._bc()
            return self._json({"seq": (bc.seq if bc else 0),
                               "enabled": bc is not None})
        if p == "/api/tts/next":
            return self._json(self._tts_next(q))
        if p.startswith("/api/tts/audio/"):
            return self._tts_audio(p)
        return self._send(404, "not found")

    # ── POST ───────────────────────────────────────────────────────
    def do_POST(self):
        p = urlparse(self.path).path
        self._bump()
        d = self._body()
        ctrl = State.ctrl

        # 急停：**第一条分支**，不排队、不判断其他状态
        if p == "/api/estop":
            try:
                ctrl.trigger_emergency(reason="网页急停", announce=True)
                with State.lock:
                    State.hold = False
                _emit("estop", reason="网页急停")
                return self._json({"ok": True, "msg": "已急停并锁定"})
            except Exception as e:
                return self._json({"ok": False, "msg": "急停失败：%s" % e}, 500)

        if p == "/api/estop/release":
            try:
                ctrl.release_estop(announce=True)
                _emit("release")
                return self._json({"ok": True, "msg": "已解除急停锁定"})
            except Exception as e:
                return self._json({"ok": False, "msg": "解除失败：%s" % e}, 500)

        # ── 远程电源（高级功能；隐藏在主界面之外）────────────────────
        # 安全约定：① 动作白名单；② 必须带 confirm；③ 先停车再掉电；
        #          ④ 只通过 setup_power.sh 安装的提权脚本执行。
        if p == "/api/power":
            act = str(d.get("action") or "")
            if act not in POWER_ACTIONS:
                return self._json({"ok": False, "msg": "不支持的操作"},
                                  400)
            if str(d.get("confirm") or "").strip().lower() not in ("yes", "确认"):
                return self._json({"ok": False, "msg": "缺少确认参数"}, 400)
            if not _power_available():
                return self._json(
                    {"ok": False,
                     "msg": "板子未安装电源助手：先在板子上跑 bash setup_power.sh"})
            ctrl.log("🛑 网页请求%s（来自 %s）" % (
                POWER_ACTIONS[act], self.client_address[0]))
            _emit("power", action=act)
            _schedule_power(act, ctrl)
            return self._json({"ok": True, "action": act,
                               "msg": "1.5 秒后%s开发板，页面会断开…"
                                      % POWER_ACTIONS[act]})

        if p == "/api/stop":
            with State.lock:
                State.hold = False
                State.hold_action = None
            try:
                ctrl.stop()
            except Exception as e:
                return self._json({"ok": False, "msg": "停车失败：%s" % e}, 500)
            return self._json({"ok": True})

        if p == "/api/action":
            return self._json(self._action(d))

        if p == "/api/speed":
            try:
                v = ctrl.set_speed_value(int(d.get("value")))
                return self._json({"ok": True, "speed": v,
                                   "speed_mmps": mmps_for_display(v)})
            except Exception as e:
                return self._json({"ok": False, "msg": "速度非法：%s" % e}, 400)

        if p == "/api/text":
            text = str(d.get("text") or "").strip()[:TEXT_MAX]
            if not text:
                return self._json({"ok": False, "msg": "内容为空"}, 400)
            ctrl.log("🌐 网页指令：%s" % text)
            turn = _new_turn()
            _emit("heard", turn, text=text,
                  src=str(d.get("src") or "text"))
            State.q.put((text, turn))
            return self._json({"ok": True, "queued": True, "turn": turn})

        if p == "/api/heartbeat":
            return self._json({"ok": True})

        if p == "/api/demo":
            on = bool(d.get("on"))
            v = ctrl.set_demo_mode(on)
            return self._json({"ok": True, "demo_mode": v,
                               "msg": "已开启离线演示（不发串口）" if v else "已关闭离线演示"})

        # ── 切换输入麦克风（阵列麦 / 无线麦）─────────────────────────
        #   作用于：按住说话 / 自动聆听 / 语音急停（KWS）。
        #   KWS 必须重启才换设备（模型 + 监听器绑定设备），约 8~10 秒
        #   不可用 ⇒ 只在**车停稳**时立即重启；车在动就等停稳后由看门狗做。
        if p == "/api/mic":
            try:
                A = _asr_mod()
                if bool(d.get("auto")):
                    A.clear_preferred_input()
                    msg = "已恢复自动选择麦克风"
                else:
                    try:
                        idx = int(d.get("index"))
                    except Exception:
                        return self._json(
                            {"ok": False, "msg": "缺少设备 index"}, 400)
                    devs = A.real_input_devices()
                    hit = next((x for x in devs
                                if int(x.get("index", -1)) == idx), None)
                    if hit is None:
                        return self._json(
                            {"ok": False,
                             "msg": "设备 %s 不在了（可能被拔掉），点「刷新」重试"
                                    % idx}, 400)
                    ident = A.save_preferred_input(idx)
                    if not ident:
                        return self._json(
                            {"ok": False, "msg": "记住麦克风失败（权限？）"},
                            500)
                    msg = "已选择：%s" % (hit.get("name") or idx)
            except Exception as e:
                return self._json({"ok": False,
                                   "msg": "切换麦克风失败：%r" % (e,)}, 500)
            with State.lock:
                listening = State.listening
            kws_on = getattr(ctrl, "_kws_state", "off") in ("ready", "loading")
            if kws_on:
                if _voice_idle():
                    _restart_voice_async(listening)
                    msg += "；语音急停正在用新麦克风重启（约 8~10 秒不可用）"
                else:
                    with State.lock:
                        State.pending_kws_mic = True
                    msg += "；小车正在运动，停稳后会自动切换语音急停"
            elif listening:
                _restart_listening_async()
                msg += "；自动聆听正在用新麦克风重启"
            ctrl.log("🎙 网页切换输入麦克风 → %s" % msg)
            return self._json({"ok": True, "msg": msg,
                               "mic": _mic_snapshot()})

        if p == "/api/voice/estop":
            return self._json(self._voice_estop(bool(d.get("on"))))
        if p == "/api/voice/wake":
            return self._json(self._voice_wake(bool(d.get("on"))))

        # ── 语音输入（**远程开启板子麦克风**）─────────────────────
        if p == "/api/voice/load":
            ok, msg = _ensure_asr()
            return self._json({"ok": True, "ready": ok,
                               "msg": msg or "语音识别已就绪"})
        if p == "/api/voice/ptt/start":
            return self._json(self._ptt_start())
        if p == "/api/voice/ptt/stop":
            return self._json(self._ptt_stop())
        if p == "/api/voice/listen":
            return self._json(self._listen(bool(d.get("on"))))
        if p == "/api/voice/autoexec":
            with State.lock:
                State.auto_exec = bool(d.get("on"))
                v = State.auto_exec
            return self._json({"ok": True, "auto_exec": v,
                               "msg": "识别后自动执行：%s" % ("开" if v else "关")})
        if p == "/api/voice/exec":
            text = str(d.get("text") or "").strip()[:TEXT_MAX]
            if not text:
                with State.lock:
                    text = State.asr_text
            if not text:
                return self._json({"ok": False, "msg": "没有可执行的文本"}, 400)
            ctrl.log("🎤 手动执行识别结果：%s" % text)
            turn = _new_turn()
            _emit("heard", turn, text=text, src="voice")
            State.q.put((text, turn))
            return self._json({"ok": True, "queued": True, "text": text,
                               "turn": turn})

        return self._send(404, "not found")

    # ── 业务实现 ───────────────────────────────────────────────────
    @staticmethod
    def _bc():
        """取网络音箱的广播对象（板子有本地喇叭时为 None）。"""
        w = getattr(State.ctrl, "_web", None)
        return w[1] if w else None

    @staticmethod
    def _status():
        ctrl = State.ctrl
        try:
            st = ctrl.status()
        except Exception as e:
            return {"ok": False, "msg": "状态读取失败：%s" % e}
        with State.lock:
            hb_age = time.time() - State.hb
            hold = State.hold
        sp = st.get("speed") or 0
        # ★ 「电脑端控制台在线？」= 最近 3 秒内有浏览器在轮询。
        #   ⚠️ 时钟被往回拨时（2026-11-03 真机：开机 RTC 把本地时间当 UTC，
        #   +8h，随后校正 → last_browser_seen 落在"未来"，console_age=-28808），
        #   旧写法 `age < 3` 会把「电脑在线」永远顶成 true。必须卡下界。
        _cage = (time.time() - State.last_browser_seen) if State.last_browser_seen else -1.0
        return {
            "ok": True,
            "connected": bool(st.get("connected")),
            "port": st.get("port") or "",
            "voltage": round(float(st.get("voltage") or 0.0), 2),
            "volt_level": st.get("volt_level"),
            "action": st.get("action"),
            "action_label": st.get("action_label"),
            "speed": sp,
            "speed_mmps": mmps_for_display(sp),
            # ── 串口反馈原始量（排障用：命令发了但车不动时，看这几项就知道
            #    "命令有没有到 MCU、MCU 有没有在转电机"）──
            "have_feedback": bool(st.get("have_feedback")),
            "stale": bool(st.get("stale")),
            "flag_stop": st.get("flag_stop"),
            "vx": st.get("vx"), "vy": st.get("vy"), "vz": st.get("vz"),
            "estop_locked": bool(st.get("estop_locked")),
            "estop_reason": st.get("estop_reason") or "",
            "demo_mode": bool(st.get("demo_mode")),
            # 远程电源按钮是否可用（没装 setup_power.sh 时前端隐藏这一块）
            "power": _power_available(),
            "plan_running": bool(getattr(ctrl, "_plan_running", False)),
            "plan_step": int(getattr(ctrl, "_plan_step", 0) or 0),
            "plan_total": int(getattr(ctrl, "_plan_total", 0) or 0),
            "plan_label": getattr(ctrl, "_plan_label", "") or "",
            "tts_state": st.get("tts_state"),
            "tts_msg": st.get("tts_msg") or "",
            "llm_state": st.get("llm_state"),
            "llm_msg": st.get("llm_msg") or "",
            "kws_state": st.get("kws_state"),
            "kws_ok": State.voice_ok,
            "wake_ok": State.wake_ok,
            # ── 语音输入（ASR）──
            "asr_state": State.asr_state,          # off/loading/ready/failed
            "asr_msg": State.asr_msg,
            "asr_ready": bool(State.asr is not None
                              and getattr(State.asr, "ready", False)),
            "asr_text": State.asr_text,
            "asr_cost": State.asr_cost,
            "asr_seq": State.asr_seq,
            "asr_level": _asr_level(),
            "listening": State.listening,
            "recording": State.recording,
            "auto_exec": State.auto_exec,
            "speaker": Handler._bc() is not None,
            # ★ 大屏展示用：最近 3 秒内有**浏览器控制台**在轮询吗？
            #   供 display.py 判断"该进演示态还是待机态"（方案 §5）。
            "console_online": 0.0 <= _cage < 3.0,
            "console_age": (round(_cage, 1) if State.last_browser_seen else -1),
            # 麦克风正被谁长期占用（"" = 没人占）——界面用它提示"语音急停已让位"
            "mic_owner": getattr(ctrl, "_mic_claim", None) or "",
            "holding": hold,
            "hb_age": round(hb_age, 1),
            "uptime": round(time.time() - State.boot, 1),
        }

    @staticmethod
    def _logs(q):
        try:
            since = int((q.get("since") or ["0"])[0])
        except Exception:
            since = 0
        with State.lock:
            lines = [t for (n, t) in State.logs if n > since]
            seq = State.log_seq
        return {"since": seq, "lines": lines}

    @staticmethod
    def _events(q):
        """结构化事件增量（网页对话卡片据此拼"听到/理解/执行/回应"）。

        `since` 语义与 `/api/logs` 一致：只返回 seq > since 的事件，
        并回带最新 seq，前端记住它下次接着取。
        """
        try:
            since = int((q.get("since") or ["0"])[0])
        except Exception:
            since = 0
        with State.lock:
            evs = [e for e in State.events if e["seq"] > since]
            seq = State.ev_seq
        # 同时给 seq（方案 §7.3 的字段名）和 since（与 /api/logs 一致），
        # 前端记哪个都行，不会因为换名字而漏事件。
        return {"seq": seq, "since": seq, "events": evs}

    @staticmethod
    def _tts_next(q):
        bc = Handler._bc()
        if bc is None:
            return {"seq": 0, "text": "", "url": ""}
        try:
            since = int((q.get("since") or ["0"])[0])
        except Exception:
            since = 0
        try:
            wait = min(25.0, float((q.get("wait") or ["20"])[0]))
        except Exception:
            wait = 20.0
        end = time.time() + wait
        while True:
            s = bc.next_ready(since)
            if s:
                it = bc.item(s) or {}
                return {"seq": s, "text": it.get("text", ""),
                        "url": "/api/tts/audio/%d.wav" % s}
            if time.time() >= end:
                return {"seq": since, "text": "", "url": ""}
            time.sleep(0.1)

    def _tts_audio(self, p):
        bc = self._bc()
        tail = p[len("/api/tts/audio/"):]
        if bc is None or not tail.endswith(".wav"):
            return self._send(404, "not found")
        try:
            seq = int(tail[:-4])
        except Exception:
            return self._send(404, "bad seq")
        it = bc.item(seq)
        if not it or not it.get("bytes"):
            return self._send(404, "音频还没合成好")
        return self._send(200, it["bytes"], "audio/wav")

    def _action(self, d):
        """下发动作。不带 duration = 持续运动（按住走），由看门狗兜底。"""
        ctrl = State.ctrl
        act = str(d.get("action") or "")
        if act not in WEB_ACTIONS:
            return {"ok": False, "msg": "不支持的动作：%s" % act}
        dur = d.get("duration")
        try:
            dur = None if dur in (None, "", "hold") else float(dur)
        except Exception:
            dur = None
        if dur is not None and not (0.05 <= dur <= 30.0):
            return {"ok": False, "msg": "时长超范围"}

        if ctrl.estop_locked and act != "stop":
            return {"ok": False, "msg": "急停锁定中，先解除锁定"}

        if act == "stop":
            with State.lock:
                State.hold = False
                State.hold_action = None
            ok = ctrl.stop()
            return {"ok": bool(ok), "msg": "已停止" if ok else "停止失败"}

        ok = ctrl.move(act, None, dur)
        if ok and dur is None:
            # 持续运动 → 交给看门狗兜底
            with State.lock:
                State.hold = True
                State.hold_action = act
                State.hb = time.time()
        return {"ok": bool(ok),
                "msg": "已下发" if ok else "下发失败（未连接或已锁定）"}

    @staticmethod
    def _voice_estop(on):
        ctrl = State.ctrl
        if on:
            try:
                ok, msg = ctrl.enable_voice_emergency()
                State.voice_ok = bool(ok)
                return {"ok": bool(ok), "msg": msg}
            except Exception as e:
                return {"ok": False, "msg": "开启语音急停失败：%s" % e}
        try:
            ctrl.disable_voice_emergency()
        except Exception as e:
            return {"ok": False, "msg": "关闭失败：%s" % e}
        State.voice_ok = False
        return {"ok": True, "msg": "已关闭语音急停"}

    @staticmethod
    def _voice_wake(on):
        ctrl = State.ctrl
        if not State.wake_ok:
            return {"ok": False, "msg": State.wake_note}
        try:
            if on:
                ok, msg = ctrl.enable_wake()
            else:
                ok, msg = ctrl.disable_wake(), "已关闭唤醒"
            return {"ok": bool(ok), "msg": msg}
        except Exception as e:
            return {"ok": False, "msg": "操作失败：%s" % e}

    # ── 语音输入：按住说话 ─────────────────────────────────────────
    @staticmethod
    def _ptt_start():
        """按下：借麦克风 → 等播报结束 → 开录。

        顺序**不能乱**（与 GUI 的 F2 流程一致）：
          ① `_yield_mic()` 让 KWS 停手并释放麦克风 —— 本板麦克风只有一个
             subdevice，不让位的话开麦必失败；
          ② 播报期间麦克风完全打不开（实测 -9999）→ 等 `playback_idle()`；
          ③ 最后才 `ptt_start()`。
        """
        ctrl = State.ctrl
        ok, msg = _ensure_asr()
        if not ok:
            return {"ok": False, "msg": msg}
        with State.lock:
            was_listening = State.listening
            State.listening = False           # 自动聆听与按住说话互斥
            already = State.recording
        if already:
            return {"ok": True, "msg": "已在录音"}
        if was_listening:
            # 自动聆听长期占着麦克风 → 先让它释放，再借给按住说话
            try:
                ctrl.release_mic("自动聆听")
            except Exception:
                pass
            State.mic_yielded = False
            time.sleep(0.4)                   # 等自动聆听循环把麦克风放掉

        try:
            State.mic_yielded = bool(ctrl._yield_mic(owner="按住说话")[0])
        except Exception:
            State.mic_yielded = False
        try:
            if not ctrl.playback_idle():
                quiet, why = ctrl.yield_speaker(max_wait=3.0)
                if why:
                    ctrl.log("🔊 录音前等播报：%s" % why)
                if not quiet:
                    ctrl._restore_mic(State.mic_yielded)
                    State.mic_yielded = False
                    return {"ok": False,
                            "msg": "播报还没结束、麦克风被占用，请稍后再按"}
        except Exception:
            pass

        try:
            ok2, msg2 = State.asr.ptt_start(device=_asr_device())
        except Exception as e:
            ok2, msg2 = False, "录音启动异常：%r" % (e,)
        if not ok2:
            try:
                ctrl._restore_mic(State.mic_yielded)
            except Exception:
                pass
            State.mic_yielded = False
            return {"ok": False, "msg": msg2}
        with State.lock:
            State.recording = True
        return {"ok": True, "msg": "录音中…"}

    @staticmethod
    def _ptt_stop():
        """松开：结束录音 → **先还麦克风** → 识别 → （可选）自动执行。"""
        ctrl = State.ctrl
        with State.lock:
            rec = State.recording
        if not rec:
            return {"ok": False, "msg": "当前没有在录音"}
        t0 = time.time()
        try:
            ok, res = State.asr.ptt_stop()            # 含识别，约 1~3 秒
        except Exception as e:
            ok, res = False, "识别异常：%r" % (e,)
        finally:
            # ★ 先还麦克风再算结果：识别是纯 CPU 计算，不需要录音设备，
            #   早还回去能让**语音急停尽早恢复**（安全优先）
            try:
                ctrl._restore_mic(State.mic_yielded)
            except Exception:
                pass
            State.mic_yielded = False
            with State.lock:
                State.recording = False
        if not ok:
            return {"ok": False, "msg": res}
        cost = time.time() - t0
        _set_recognized(res, cost)
        with State.lock:
            auto = State.auto_exec
        return {"ok": True, "text": res, "cost": round(cost, 2), "auto_exec": auto}

    # ── 语音输入：自动聆听 ─────────────────────────────────────────
    @staticmethod
    def _listen(on):
        """自动聆听开关（VAD 断句：说一句识别一句）。"""
        ctrl = State.ctrl
        if not on:
            with State.lock:
                State.listening = False
            # 交还麦克风（放后台别卡住 HTTP）
            def _give_back():
                try:
                    ctrl.release_mic("自动聆听")
                except Exception:
                    pass
                State.mic_yielded = False
            threading.Thread(target=_give_back, daemon=True).start()
            ctrl.log("🎤 自动聆听已关闭（麦克风还给语音急停）")
            return {"ok": True, "msg": "已停止自动聆听"}

        ok, msg = _ensure_asr()
        if not ok:
            return {"ok": False, "msg": msg}
        with State.lock:
            if State.listening:
                return {"ok": True, "msg": "已在自动聆听"}
            was_rec = State.recording
        if was_rec:
            Handler._ptt_stop()               # 先把按住的录音收尾
        # ★ 自动聆听是"反复开关麦克风"的循环 → 必须**长期占用**（claim）。
        #   只让位一次不够：中间只要播报一次，`_restore_mic()` 就会把 KWS
        #   拉起来把麦克风抢回去，下一句就录不进（真机症状：日志刷
        #   「麦克风被占用…稍后自动重试」，自动聆听永远用不了）。
        ok, why = False, ""
        try:
            ok, why = ctrl.claim_mic("自动聆听")
        except Exception as e:
            why = "占用麦克风异常：%r" % (e,)
        if why:
            ctrl.log("🎤 自动聆听前：%s" % why)
        if not ok and why and "占用" in why:
            # 别人正拿着麦克风 → 直接告诉用户，别假装已经开始了
            return {"ok": False, "msg": why}
        State.mic_yielded = bool(ok)
        with State.lock:
            State.listening = True
        threading.Thread(target=_listen_worker, daemon=True,
                         name="web-listen").start()
        ctrl.log("🎤 开始自动聆听（停顿后自动识别%s）"
                 % ("；语音急停已让位" if ok else "；语音急停未在跑"))
        return {"ok": True, "msg": "已开始自动聆听，请对着麦克风说话"}


# ══════════════════════════════════════════════════════════════════════
#  启动
# ══════════════════════════════════════════════════════════════════════
def _load_ui():
    try:
        with open(UI_FILE, encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return ("<h1>缺少界面文件 web_ui.html</h1><p>%s</p>"
                "<p>请把 <code>web_ui.html</code> 和 <code>car_web.py</code> "
                "放在同一目录。</p>" % e)


def main():
    ap = argparse.ArgumentParser(description="无界面远程 Web 控制台")
    ap.add_argument("--port", type=int, default=8090, help="控制台端口（默认 8090）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址")
    ap.add_argument("--serial", default=None, help="串口（默认自动找 /dev/ttyCAR）")
    ap.add_argument("--no-voice", action="store_true",
                    help="不开语音急停（不占用麦克风）")
    ap.add_argument("--asr", action="store_true",
                    help="启动时就预加载语音识别（默认改为按需：网页上点「加载语音识别」）")
    ap.add_argument("--no-tts", action="store_true", help="不加载语音合成")
    ap.add_argument("--tts-web-port", type=int, default=8099,
                    help="网络音箱端口（默认 8099）")
    ap.add_argument("--demo", action="store_true",
                    help="离线演示模式：照常应答但不发串口")
    ap.add_argument("--llm-mode", default="auto",
                    choices=["auto", "api", "server", "cli", "off"],
                    help="LLM 后端（默认 auto：有 key 走云端）")
    a = ap.parse_args()

    print("=" * 68)
    print("  小车 · 远程 Web 控制台")
    print("=" * 68)

    ctrl = C.CarController()
    State.ctrl = ctrl
    State.ui = _load_ui()

    if a.demo:
        ctrl.set_demo_mode(True)

    # LLM（文本指令的纠错/闲聊）
    if a.llm_mode != "off":
        try:
            ok, msg = ctrl.init_llm(mode=a.llm_mode)
            print("  LLM：%s" % msg)
        except Exception as e:
            print("  LLM 初始化失败：%r（不影响手动控制）" % (e,))
    else:
        print("  LLM：已按参数关闭")

    # TTS（网页当喇叭要它合成）
    if not a.no_tts:
        try:
            ctrl.init_tts(background=True, web_port=a.tts_web_port)
            print("  TTS：后台加载中（进度见网页状态）")
            # ★ 保证浏览器一定能听到：本板那个 USB 声卡只能录音，
            #   而 output_probe() 会误判成"有喇叭"→ 网络音箱不启动 → 全哑。
            threading.Thread(target=_ensure_web_speaker,
                             args=(ctrl, a.tts_web_port), daemon=True,
                             name="web-speaker").start()
        except Exception as e:
            print("  TTS 初始化失败：%r" % (e,))
    else:
        print("  TTS：已按参数关闭")

    # 语音急停（KWS）
    if not a.no_voice:
        try:
            ok, msg = ctrl.enable_voice_emergency()
            State.voice_ok = bool(ok)
            print("  语音急停：%s" % msg)
        except Exception as e:
            print("  语音急停失败：%r（网页控制不受影响）" % (e,))
    else:
        print("  语音急停：已按参数关闭")

    # 语音识别（ASR）
    #   · 默认**懒加载**：在网页上点「加载语音识别」时才加载
    #     （省内存、开机快；能否用语音是用户临时决定的）
    #   · `--asr` = 启动就预加载（想一开机就能按住说话时用）
    if a.asr:
        print("  语音识别：启动预加载中（228MB，约 10~30 秒，进度见网页）…")
        threading.Thread(target=_asr_load_worker, daemon=True,
                         name="web-asr-preload").start()
    else:
        print("  语音识别：按需加载（网页上点「加载语音识别」即可）")

    # 后台线程
    for fn, name in ((_log_pump, "web-log"), (_watchdog, "web-watchdog"),
                     (_text_worker, "web-text"), (_plan_watcher, "web-plan")):
        threading.Thread(target=fn, daemon=True, name=name).start()
    threading.Thread(target=_autoconnect, args=(ctrl, a.serial),
                     daemon=True, name="web-connect").start()

    # HTTP
    try:
        srv = QuietServer((a.host, a.port), Handler)
    except OSError as e:
        print("  ❌ 端口 %d 起不来：%s" % (a.port, e))
        return 1
    srv.daemon_threads = True

    ip = "127.0.0.1"
    try:
        import tts_serve as _T
        ip = _T.local_ip()
    except Exception:
        pass
    print()
    print("  ✅ 控制台已启动：")
    print("     浏览器打开   http://%s:%d/" % (ip, a.port))
    if not a.no_tts:
        print("     （车里说话时，网页「高级 → 浏览器当喇叭」开一下就能听到）")
    print()
    print("  Ctrl+C 退出")
    print("=" * 68)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  正在退出…")
    finally:
        try:
            ctrl.stop()
        except Exception:
            pass
        for fn in (ctrl.shutdown_wake, ctrl.disable_voice_emergency,
                   ctrl.shutdown_tts, ctrl.shutdown_llm):
            try:
                fn()
            except Exception:
                pass
        try:
            ctrl.disconnect()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())