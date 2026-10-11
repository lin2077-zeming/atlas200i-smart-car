# -*- coding: utf-8 -*-
"""
car_gui_core.py —— 小车 GUI 的业务核心（与界面框架无关）

设计原则
--------
1. **不修改**原有的 car_serial.py / motion_control.py，只做导入与调用。
2. **不依赖任何 GUI 库**，因此 PySide6 版和 Tkinter 版可以共用同一份逻辑。
3. **绝不阻塞界面线程**：所有带 duration 的定时停车用后台 Timer 完成，
   不使用 MotionController 里带 time.sleep 的方法（那会卡死 GUI）。

单独自测（不需要界面）：

    python3 car_gui_core.py                          # 打印 5 秒状态
    python3 car_gui_core.py --port /dev/ttyCAR       # 指定串口
    python3 car_gui_core.py --port /dev/ttyCAR --demo  # 跑一遍前进/后退/转向
"""

import os
import re
import sys
import threading
import time
from collections import deque
from queue import Queue, Empty

# ── 让 car_serial / motion_control 能被找到 ──────────────────────────────
# 支持两种摆放方式：三个 .py 与本文件同目录，或放在上一级目录
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if os.path.exists(os.path.join(_p, "car_serial.py")):
        if _p not in sys.path:
            sys.path.insert(0, _p)
        break

# ── 可选依赖探测（缺了不崩，只报友好提示）───────────────────────────────
try:
    from serial.tools import list_ports as _serial_list_ports
    HAS_PYSERIAL = True
except Exception:
    HAS_PYSERIAL = False

HAS_CAR_MODULE = False
CAR_MODULE_ERROR = ""
try:
    from car_serial import CarSerial            # noqa: E402
    from motion_control import MotionController  # noqa: E402
    import car_serial as _car_serial             # 供 _FastReadCarSerial 复用帧工具
    HAS_CAR_MODULE = True
except Exception as _e:                          # pragma: no cover
    CAR_MODULE_ERROR = str(_e)

# ── 可选依赖：TTS（语音播报）/ KWS（语音急停）────────────────────────────
# 这两个缺了只意味着"没声音 / 没语音急停"，**不影响手动控制**，
# 所以全部用 try 包住 —— 绝不能让语音模块的问题把控制器一起拖崩。
HAS_TTS = False
TTS_ERROR = ""
try:
    import tts_engine as _tts_engine                    # noqa: E402
    HAS_TTS = True
except Exception as _e:                                 # pragma: no cover
    _tts_engine = None
    TTS_ERROR = str(_e)

HAS_KWS = False
KWS_ERROR = ""
try:
    import kws_engine as _kws_engine                    # noqa: E402
    HAS_KWS = True
except Exception as _e:                                 # pragma: no cover
    _kws_engine = None
    KWS_ERROR = str(_e)

# ── 可选依赖：本地 LLM（指令纠错 + 拟人化对话）────────────────────────
# 定位：项目重点是**人机交互**，LLM 用来补"ASR 听错 → 没听懂"和"回复太生硬"。
# ⚠️ 它是**增强**不是**依赖**：缺失/超时/报错都只意味着"退回原来的没听懂提示"，
#    绝不影响规则快路径、解析、播报与安全链路。
HAS_LLM = False
LLM_ERROR = ""
try:
    import llm_engine as _llm_engine                    # noqa: E402
    HAS_LLM = True
except Exception as _e:                                 # pragma: no cover
    _llm_engine = None
    LLM_ERROR = str(_e)

#: LLM 给出指令时的最低置信度。低于它就只当闲聊（**宁可看不懂，不要乱动车**）。
#:
#: 🔴 2026-09-27 用 14 条**真实 ASR 输出**实测后定的值，完整推理见
#:   `llm_engine.LLM_CMD_MIN_CONFIDENCE` 与 `ACTION_EVIDENCE` 的注释：
#:   · 0.5B 拿不准就猜"前进"，垃圾输入也自称 0.9~0.95；
#:   · 置信度**区分不了**合法与垃圾（合法口语也才 0.90）
#:     → 抬到 0.95 会误伤 `开慢点儿`，所以只取 0.8 做粗筛；
#:   · **真正的闸门是 `llm_engine.cmd_has_evidence()`**（要求字面证据），
#:     它在引擎内部执行，这里这道只是双保险。
#:   ⚠️ **不要为了"多识别几条"而降这个值** —— 乱动的代价远大于漏掉。
LLM_MIN_CONFIDENCE = 0.8


#: 串口"读排空"的轮询间隔（秒）。见 `_FastReadCarSerial` 的说明。
SERIAL_DRAIN_INTERVAL = 0.002


class _FastReadCarSerial(CarSerial if HAS_CAR_MODULE else object):
    """
    读取更勤快的 CarSerial —— 绕开 vendor 版"读被饿死"的问题。

    🔴 为什么要这个子类（2026-09-27 实机实测，证据链完整）
    ------------------------------------------------------
    vendor 版 `car_serial._sender_loop` 是「**写一次 → 读一次 → 睡 50ms**」。
    实测在**持续下发指令**时，下位机的回传会**整段断掉**：

        · 发「前进 200」期间，连续 2 秒收到 **0 帧**；
        · 厂商自带自测 `python3 car_serial.py`（前进 3 秒打印 Vx）
          **一行都打不出来**，同样是这个原因；
        · 表现到车上是——**轮子转得很慢、断断续续**。

    因果链：读得慢 → 下位机回传被堵 → 它的控制循环被拖慢 →
    下位机"1 秒收不到新指令就把 Move_XYZ 归零"的超时反复触发 →
    走走停停。

    改成**独立的 2ms 高频排空**后实测（同一台车、同一条线）：

        3 秒收到 **60 帧**（理想 60 帧，20 帧/秒，**零丢帧**）
        Vx 峰值 **226**（命令 200，量纲对得上）

    ⚠️ `car_serial.py` 是**只读文件**（项目约定：不动 vendor 代码），
       所以用**子类覆盖 `_sender_loop`** 打补丁，原文件一个字节不改。
       风险：若日后 vendor 更新 `car_serial.py` 的逻辑，这里需要同步跟进。
    """

    def _sender_loop(self):
        build_command = _car_serial.build_command
        parse_feedback = _car_serial.parse_feedback
        head = _car_serial.FRAME_HEAD
        tail = _car_serial.FRAME_TAIL

        buf = bytearray()
        last_send = 0.0
        while self._running:
            try:
                now = time.time()
                # ① 仍然保持 20Hz 下发（与 vendor 一致）
                if now - last_send >= 0.05:
                    last_send = now
                    with self._lock:
                        cmd = build_command(self._x, self._y, self._z)
                    self._ser.write(cmd)

                # ② **高频排空**：这是与 vendor 版唯一的实质区别
                n = self._ser.in_waiting
                if n:
                    buf.extend(self._ser.read(min(n, 512)))

                # ③ 解析（逻辑与 vendor 一致）
                while len(buf) >= 24:
                    if buf[0] == head and buf[23] == tail:
                        fb = parse_feedback(bytes(buf[:24]))
                        if fb is not None:
                            with self._fb_lock:
                                self._feedback = fb
                        buf = buf[24:]
                    else:
                        idx = buf.find(head, 1)
                        if idx > 0:
                            buf = buf[idx:]
                        elif len(buf) > 48:
                            buf.clear()
                        else:
                            break
                time.sleep(SERIAL_DRAIN_INTERVAL)
            except Exception:
                time.sleep(0.5)


# ── 常量 ────────────────────────────────────────────────────────────────
APP_NAME = "智能小车手动控制台"
APP_VERSION = "1.0-manual"

DEFAULT_BAUD = 115200
SPEED_MIN = 50
SPEED_MAX = 500
SPEED_STEP = 50
DEFAULT_SPEED = 300

# ── 电池电压阈值 ────────────────────────────────────────────────────────
# 本车为 12V 系统（实测数据线上报约 12V，见 12V 控制板说明书第 18 页示例：
# 0x58 0x38 = 22584 mV）。厂商《T800S Pro 航模遥控器操作说明书》第 9 页明确：
#   "使用时需特别注意电池电压，电池电压不能低于 10V。低于 10V 请停止使用，
#    关机充电后再使用。如果电池电压低于 10V 后再充电，容易造成电池损伤。"
# 因此 10.0 V 是**厂商规定的绝对下限**（红色告警 + 视为锁死），
# 而非"下位机自己在 10.8V 锁死"——下位机的 Turn_Off() 只管 10V 这一条线。
VOLT_WARN = 11.0          # 低于此值黄色告警（12V 系统留 ~1V 余量）
VOLT_LOCK = 10.0          # 低于此值红色告警；同时是厂商规定的电池安全下限

# 出厂/常见串口设备名，按优先级排列
PORT_CANDIDATES = (
    "/dev/ttyCAR",        # 推荐的 udev 固定别名
    "/dev/ttyUSB0",       # CH9102 / CH343 / CH340 常见名（都走 ch341 驱动）
    "/dev/ttyUSB1",
    "/dev/ttyACM0",       # 原代码默认名（CDC-ACM 设备）
    "/dev/ttyACM1",
)

# 动作 → 三轴速度方向系数（与 car_serial.set_speed 的 x/y/z 语义对应）
#   x>0 前进  x<0 后退
#   y>0 左移  y<0 右移   （仅麦轮/全向轮支持；四驱、差速、阿克曼、履带车不支持）
#   z>0 左转（逆时针）   z<0 右转（顺时针）
#
# 三轴方向标定（12V 控制板说明书第 16 页"机器人 XYZ 三轴方向标定"）：
#   X 轴正方向 = 前进，Y 轴正方向 = 左移，Z 轴正方向 = **逆时针（即左转）**
#
# ✅ 2026-09-27 **实机目视标定确认：说明书是对的。**
#    下发 z>0 时小车确实逆时针（左）转。
#
# 🔴 历史坑（已修）：本文件早期按 car_serial.py 的注释写成"z>0 右转"，
#    与说明书**正好相反** → 按界面「左转」小车实际**顺时针（右）转**。
#    船长实机目视发现后改正。
#    **教训：说明书和代码注释冲突时，以实机实测为准。**
LEFT_RIGHT_SWAP = False   # 万一将来换车/换固件又反了，置 True 一键翻转

ACTION_XYZ = {
    "forward":      (1, 0, 0),
    "backward":     (-1, 0, 0),
    "turn_left":    (0, 0, 1),      # Z+ = 逆时针 = 左转（实机确认）
    "turn_right":   (0, 0, -1),
    "strafe_left":  (0, 1, 0),
    "strafe_right": (0, -1, 0),
    "stop":         (0, 0, 0),
}

# ⚠️ `LEFT_RIGHT_SWAP` 必须**真的作用到 ACTION_XYZ 上**才有意义。
#    它早期只是"定义了却从没被任何代码引用" —— 于是文档和测试里写的
#    "改这个开关一键翻转"是句**空话**，改了毫无效果（2026-09-27 踩到）。
#    现在把它真正接上。
if LEFT_RIGHT_SWAP:
    ACTION_XYZ["turn_left"], ACTION_XYZ["turn_right"] = (
        ACTION_XYZ["turn_right"], ACTION_XYZ["turn_left"])

# 指令栏的「常用指令」下拉预设。
#
# 存在的意义（2026-09-27 踩坑后加）：
#   界面里输入中文**依赖系统输入法**。本板出厂预装的是 fcitx4，但
#   PySide6 = Qt6 只有 ibus 插件、没有 fcitx 插件 → 中文打不出来
#   （解法见 setup_ibus.sh）。万一输入法没配好，至少还有这个下拉可用，
#   而且点选比打字快，演示时更好用。
# ⚠️ 新增条目后请跑 test_e2e_voice.py 确认都能被 parse_text_command 解析。
COMMON_COMMANDS = (
    "前进一米",
    "后退一米",
    "前进 2 秒",
    "后退 2 秒",
    "左转90度",
    "右转90度",
    "左转 1 秒",
    "右转 1 秒",
    "速度 200",
    "快一点",
    "慢一点",
    "停止",
)

ACTION_LABEL = {
    "forward":      "前进",
    "backward":     "后退",
    "turn_left":    "左转",
    "turn_right":   "右转",
    "strafe_left":  "左移",
    "strafe_right": "右移",
    "stop":         "停止",
}

# ── 语音播报模板 ────────────────────────────────────────────────────────
# ⚠️ **播报语必须短**。本板 TTS 是纯 CPU 推理，RTF ≈ 5
#    （合成 5 秒才产出 1 秒语音）。一句"已急停"要合成约 6 秒，
#    换成长句会让人等得不耐烦，也会把播报队列拖得远远落后于现实。
SPEAK_TEMPLATES = {
    "forward":      "开始前进",
    "backward":     "开始后退",
    "turn_left":    "向左转",
    "turn_right":   "向右转",
    "strafe_left":  "向左平移",
    "strafe_right": "向右平移",
    "stop":         "已停止",
}
SPEAK_ESTOP = "已急停"          # 语音急停命中
SPEAK_RELEASED = "急停已解除"    # 人工解除锁定
SPEAK_READY = "系统就绪"        # 语音链路就绪

#: 同一条播报的最小间隔（秒）。防止连续命中把队列刷爆。
SPEAK_COOLDOWN = 1.2

#: 速度播报的单位读法。
#: ⚠️ 必须有单位，否则用户听到「速度三百五十」会一愣：三百五十什么？
#:   （本板 TTS 读不出 "mm/s" 这种符号，所以这里直接写中文；
#:     符号→中文的兜底在 `npu_tts_engine.normalize_text()` 里。）
SPEED_UNIT_CN = "毫米每秒"

# ── 播报文本规范化：阿拉伯数字 → 中文 ──────────────────────────────
#: 为什么必须做（2026-09-30 用户实测报的 bug）：
#:   NPU TTS 用的是 **FastSpeech2 + 拼音词典**，流程是「汉字 → 拼音 → 音素」。
#:   而**阿拉伯数字没有拼音映射**，会被静默转成 `sp`（静音）——
#:   实测预处理结果：
#:       "加速到313"       → {j ia1 s u4 d ao4 sp}          ← "313" 没了！
#:       "313"             → {sp}                          ← 整句只剩静音
#:       "加速到三百一十三"  → {j ia1 s u4 d ao4 s an1 ...}   ← 正常
#:   所以播报「加速到313」时，用户只听到「加速到」就**戛然而止**。
#:   （旧版 Kokoro 能读数字，换 NPU 后端后这个差异才暴露出来。）
#: ⇒ 所有**带数字的播报**都要先过 `speak_text()` 转成中文数字。
#:   对能读数字的后端（sherpa/Kokoro）中文数字同样读得对，**无损**。
_CN_DIGITS = "零一二三四五六七八九"


def num_to_cn(n):
    """整数 → 中文读法（覆盖 0~9999，足够速度/距离用）。

    例：313 → "三百一十三"、250 → "二百五十"、100 → "一百"、15 → "十五"
    """
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return str(n)
    if n < 0:
        return "负" + num_to_cn(-n)
    if n == 0:
        return "零"
    if n > 9999:
        return str(n)                   # 超范围不硬转，交回原样

    units = ["", "十", "百", "千"]
    parts, s, pos = [], str(n), len(str(n))
    for i, ch in enumerate(s):
        d = int(ch)
        u = units[pos - 1 - i]
        if d == 0:
            parts.append("")            # 占位，处理"零"
        elif d == 1 and u == "十" and i == 0:
            parts.append("十")          # 15 → "十五"（不是"一十五"）
        else:
            parts.append(_CN_DIGITS[d] + u)
    out = ""
    for i, p in enumerate(parts):
        if p:
            # 中间跳过的 0 补一个"零"（如 105 → 一百零五）
            if i > 0 and not parts[i - 1] and out and not out.endswith("零"):
                out += "零"
            out += p
    return out


def speak_text(text):
    """把播报文本里的阿拉伯数字转成中文数字（见上方 `_CN_DIGITS` 的说明）。"""
    if not text:
        return text
    import re as _re
    return _re.sub(r"\d+", lambda m: num_to_cn(m.group()), str(text))


# ── 标定系数（实机需重新校准）────────────────────────────────────────────
MM_PER_METRE = 1000.0
# 前进一米 ≈ 300mm/s 持续 3.33s  →  duration = 距离(mm) / speed
#
# ⚠️ 关于转向：下面的 DEG_PER_SEC_PER_UNIT = 0.375 是**上一代凭经验估的**，
#    用于把"左转 90 度"换算成"转多久"。但从 12V 控制板的源码看，
#    Z 轴控制量的物理意义是**角速度，单位 0.001 rad/s**（不是 mm/s）：
#      - 说明书第 18 页示例：Z 轴速度 0x0000 -> 角速度 0 "0.001rad/s"
#      - 固件 data_transition() 里 Z_speed 要除以轮距+轴距再乘 1000
#    也就是说 speed=200 对应的角速度就是 0.2 rad/s ≈ 11.46 度/秒，
#    而旧系数 0.375 度/秒对应 speed=200 只有 75 度/秒的 1/100 量级（见下）。
#    正确关系应为：角速度(度/秒) = Z控制量 × 0.001 × 180/π ≈ Z控制量 × 0.0573
#    因此这里改用**按物理量换算**，并保留旧值作为对照，实机务必标定确认。
DEG_PER_SEC_PER_UNIT = 0.375   # ⚠️ 旧经验值（保留兼容），建议实机改为 0.0573
DEG_PER_SEC_PER_UNIT_RAD = 0.001 * 180.0 / 3.141592653589793  # ≈0.05730 理论值


def guess_default_port():
    """挑一个最可能存在的串口设备名。"""
    for p in PORT_CANDIDATES:
        if os.path.exists(p):
            return p
    return PORT_CANDIDATES[0]


def list_serial_ports():
    """列出系统中可用的串口设备名。"""
    if not HAS_PYSERIAL:
        return []
    try:
        ports = {p.device for p in _serial_list_ports.comports()}
        # 补上可能存在但 pyserial 没枚举到的
        for p in PORT_CANDIDATES:
            if os.path.exists(p):
                ports.add(p)
        return sorted(ports)
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════
#  文本指令解析（规则快路径；将来接 LLM 时作为它的前置/兜底）
# ══════════════════════════════════════════════════════════════════════
_CN_NUM = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}
# 数字与中文数词的字符类，供正则复用
_NUM_PAT = r"[\d.]+|[零一二两三四五六七八九十百千半]+"
_NUM_GROUP = r"(%s)" % _NUM_PAT


def _to_number(text):
    """把 '1.5' / '3' / '三' / '十五' / '三百' / '半' 解析成 float；失败返回 None。"""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass

    if text == "半":
        return 0.5

    # 纯中文数词：按 位值 累加，支持 十/百/千
    if all((ch in _CN_NUM) or (ch in _CN_UNIT) for ch in text):
        section = 0.0
        cur = 0
        seen = False
        for ch in text:
            if ch in _CN_NUM:
                cur = _CN_NUM[ch]
                seen = True
            else:
                unit = _CN_UNIT[ch]
                if cur == 0 and unit == 10:
                    cur = 1          # "十五" -> 15
                section += cur * unit
                cur = 0
                seen = True
        if not seen:
            return None
        total = section + cur
        return float(total)
    return None


_STOP_WORDS = ("急停", "停止", "停下", "停车", "别动", "刹车", "不要动", "停")
_ACTION_RULES = (
    ("turn_left",    ("左转", "往左拐", "向左转", "左拐", "逆时针")),
    ("turn_right",   ("右转", "往右拐", "向右转", "右拐", "顺时针")),
    ("strafe_left",  ("左移", "向左平移", "平移左")),
    ("strafe_right", ("右移", "向右平移", "平移右")),
    ("backward",     ("后退", "往后倒", "向后", "倒车", "退后", "后退一点")),
    ("forward",      ("前进", "往前走", "向前", "往前走一点", "往前")),
)


def parse_text_command(text, default_speed=DEFAULT_SPEED):
    """
    规则解析文本指令。

    返回 (result, message)：
      result 为 dict 时表示可执行的动作：
        {"action": "forward", "speed": 300, "duration": 3.33}
        {"action": "set_speed", "value": 200}
        {"action": "speed_up"} / {"action": "speed_down"}
      result 为 None 表示没听懂，message 是给用户的提示。
    """
    if not text or not text.strip():
        return None, "空指令"

    t = text.strip()
    low = t.lower()

    # 1) 急停 / 停止（最高优先级，且单独成词时优先命中）
    for w in _STOP_WORDS:
        if w in t:
            return {"action": "stop", "speed": 0, "duration": 0}, "急停"

    # 2) 快一点 / 慢一点
    if any(w in t for w in ("快一点", "快点", "加速", "快一些")):
        return {"action": "speed_up"}, "加速"
    if any(w in t for w in ("慢一点", "慢点", "减速", "慢一些")):
        return {"action": "speed_down"}, "减速"

    # 3) 运动指令 + 可选量词
    #    必须放在"直接设速度"之前判断，否则 "以两百的速度后退两秒"
    #    会被误判成 "设速度为 2"。
    for action, words in _ACTION_RULES:
        if not any(w in t for w in words):
            continue

        speed = default_speed
        duration = None

        # 4a) 速度：以 200 的速度 / 以三百的速度
        m = re.search(_NUM_GROUP + r"\s*(?:的)?\s*速度", t)
        if m:
            v = _to_number(m.group(1))
            if v is not None:
                speed = int(v)

        # 4b) 时长：3 秒 / 三秒 / 半秒
        m = re.search(_NUM_GROUP + r"\s*秒", t)
        if m:
            v = _to_number(m.group(1))
            if v is not None:
                duration = float(v)

        # 4c) 距离：一米 / 1.5m / 200毫米（换算成时长）
        if duration is None:
            m = re.search(_NUM_GROUP + r"\s*(?:毫米|mm)", t)
            if m:
                v = _to_number(m.group(1))
                if v is not None:
                    duration = v / max(speed, 1)
            else:
                m = re.search(_NUM_GROUP + r"\s*(?:米|m|M)(?![毫mM米])", t)
                if m:
                    v = _to_number(m.group(1))
                    if v is not None:
                        duration = (v * MM_PER_METRE) / max(speed, 1)

        # 4d) 角度：90度 / 90°（换算成时长）
        if duration is None:
            m = re.search(_NUM_GROUP + r"\s*(?:度|°|deg)", t)
            if m:
                v = _to_number(m.group(1))
                if v is not None:
                    duration = v / max(speed * DEG_PER_SEC_PER_UNIT, 1e-6)

        speed = _clamp_speed(speed)
        if duration is not None:
            duration = round(min(max(duration, 0.1), 30.0), 2)

        return {"action": action, "speed": speed, "duration": duration}, ACTION_LABEL[action]

    # 4) 前面都没匹配到动作词，才尝试"直接设速度"
    #    （要求数字紧跟在"速度"或"速度调到"后面，避免抢走别的句子里的数字）
    m = re.search(r"速度\s*(?:调到|设为|设成|改到|改成|到|为|是|[:：])?\s*" + _NUM_GROUP, t)
    if m:
        v = _to_number(m.group(1))
        if v is not None:
            return {"action": "set_speed", "value": int(v)}, "设定速度"

    # 5) 没听懂
    if any(w in low for w in ("你好", "hello", "hi")):
        return None, "你好，可以试试「前进」「后退一秒」「左转90度」「停止」"

    return None, "没听懂：%s" % t


def _clamp_speed(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = DEFAULT_SPEED
    return max(SPEED_MIN, min(SPEED_MAX, v))


# ══════════════════════════════════════════════════════════════════════
#  控制器
# ══════════════════════════════════════════════════════════════════════
class CarController(object):
    """小车控制器：串口连接、运动下发、状态读取、日志。

    线程安全约定：
      - 公开方法都可以从界面线程直接调用；
      - status() 返回一份快照 dict，不会持有锁；
      - 定时停车用 threading.Timer，不阻塞调用方。
    """

    def __init__(self):
        self._car = None
        self._mc = None
        self._timer = None
        self._timer_lock = threading.Lock()

        self._lock = threading.Lock()
        self._port = ""
        self._baud = DEFAULT_BAUD
        self._connected = False

        self._speed = DEFAULT_SPEED
        self._action = "stop"
        self._last_error = ""
        self._last_feedback_time = 0.0

        self._log = deque(maxlen=600)
        self._log_lock = threading.Lock()

        # ── 语音：TTS 播报 ──────────────────────────────────────────
        self._tts = None                # TtsEngine 实例
        self._speak_q = None            # SpeakQueue（后台串行播报）
        self._tts_state = "off"         # off / loading / ready / failed
        self._tts_msg = ""
        self._tts_backend = ""          # "" / "npu" / "cpu"（TTS 实际用的后端）
        self._tts_speaker = None        # None = 用模型默认（Kokoro → 47）
        self._speak_enabled = True      # 播报总开关（演示时嫌吵可关）
        self._announce_actions = True   # 是否播报"开始前进"这类动作确认
        self._last_speak = {}           # 文本 → 上次播报时刻（做冷却）
        self._web = None                # 网络音箱 (server, broadcaster, port)
        # 离线（演示/对话）模式：没连小车也接受指令 —— 更新本地状态并语音应答，
        # 但不产生任何串口动作。专供"人不在实验室、只测语音链路"的场景。
        self._demo = False

        # ── 语音：KWS 急停 ──────────────────────────────────────────
        self._kws = None
        self._kws_state = "off"         # off / loading / ready / failed
        self._kws_msg = ""
        self._kws_yielded = False       # 是否为了录音把 KWS 临时让位了
        #: ★ 麦克风是不是正被"录音"拿着（2026-10-11）。
        #:   播报前后的"暂停 KWS"钩子靠它判断：录音期间别去抢麦克风，
        #:   否则会把 KWS 恢复到正在录音的麦克风上（硬件互斥，必冲突）。
        self._mic_owned_by_record = False
        self._kws_hits = deque(maxlen=50)      # 命中历史 [(标签, 时刻)]
        self._voice_events = deque(maxlen=50)  # 供界面轮询的事件队列

        # ── 急停锁定态 ──────────────────────────────────────────────
        # 语音（或手动）急停后**不自动恢复**，必须人工 release_estop()。
        # 理由：误触发之后车不会自己又跑起来，这是安全底线。
        self._estop_locked = False
        self._estop_reason = ""

        # ── LLM（指令纠错 + 对话）—— 本地或云端 ─────────────────────
        # 只在**规则快路径没命中**时才被调用（见 run_text_command）。
        self._llm = None
        self._llm_state = "off"         # off / ready / failed
        self._llm_msg = ""
        self._llm_enabled = True        # 总开关（界面可关）
        self._llm_mode = "auto"         # auto / api / server / cli
        self._llm_replies = deque(maxlen=30)   # 近期对话，供界面展示

    # ── 日志 ────────────────────────────────────────────────────────
    def _append_log(self, text):
        stamp = time.strftime("%H:%M:%S")
        with self._log_lock:
            self._log.append("[%s] %s" % (stamp, text))

    def log(self, text):
        self._append_log(text)
        # 同步打到 stdout，SSH 里跑也能看到
        try:
            print("[%s] %s" % (time.strftime("%H:%M:%S"), text), flush=True)
        except Exception:
            pass

    def drain_logs(self):
        """取出并清空日志缓冲（界面定时器调用）。"""
        with self._log_lock:
            items = list(self._log)
            self._log.clear()
        return items

    # ── 连接管理 ────────────────────────────────────────────────────
    @property
    def port(self):
        return self._port

    @property
    def speed(self):
        return self._speed

    @property
    def action(self):
        return self._action

    @property
    def connected(self):
        return self._connected

    def connect(self, port=None, baud=DEFAULT_BAUD):
        """打开串口。返回 (是否成功, 提示信息)。"""
        if not HAS_CAR_MODULE:
            msg = "找不到/无法导入 car_serial.py 或 motion_control.py：%s" % CAR_MODULE_ERROR
            self.log(msg)
            return False, msg
        if not HAS_PYSERIAL:
            msg = "缺少 pyserial，请执行：sudo apt install -y python3-serial"
            self.log(msg)
            return False, msg

        self.disconnect()

        port = port or guess_default_port()
        try:
            # 用 _FastReadCarSerial（子类）而不是 CarSerial：
            # vendor 版读得太慢，持续下发指令时下位机回传会断，车就走走停停。
            # 详见 _FastReadCarSerial 的注释。
            car = _FastReadCarSerial(port, baud)
            car.open()
        except Exception as e:
            self._last_error = str(e)
            msg = "打开串口 %s 失败：%s" % (port, e)
            self.log(msg)
            return False, msg

        self._car = car
        self._mc = MotionController(car)
        self._port = port
        self._baud = baud
        self._connected = True
        self._speed = DEFAULT_SPEED
        self._action = "stop"

        self.log("已连接 %s @ %d" % (port, baud))
        self.log("已下发初始停止指令，等待状态帧…")
        return True, "连接成功"

    def disconnect(self):
        """关闭串口（CarSerial.close 会自动补发停止指令）。"""
        self._cancel_timer()
        car = self._car
        self._car = None
        self._mc = None
        self._connected = False
        self._action = "stop"
        if car is not None:
            try:
                car.close()
            except Exception:
                pass
            self.log("已断开串口 %s" % self._port)

    # ── 速度 ────────────────────────────────────────────────────────
    def set_speed_value(self, value):
        """设置默认/当前速度（mm/s），已做安全钳位。"""
        self._speed = _clamp_speed(value)
        return self._speed

    def speed_up(self, step=SPEED_STEP):
        return self.set_speed_value(self._speed + step)

    def speed_down(self, step=SPEED_STEP):
        return self.set_speed_value(self._speed - step)

    # ── 运动 ────────────────────────────────────────────────────────
    def _write_speed(self, kx, ky, kz):
        """
        所有对串口的**写**都走这里，用 `self._lock` 串行化。

        ⚠️ 现在有**两个线程**会写串口：界面线程（手动控制）和 KWS 监听线程
        （语音急停）。不串行化的话两条 11 字节帧可能交错，下位机 BCC 校验
        不过就整帧丢弃 —— 万一丢的正好是急停帧，代价太大。
        锁的粒度是"写完就放"，不会把急停放后面排队。
        """
        with self._lock:
            self._car.set_speed(kx, ky, kz)

    def _announce(self, action):
        """
        动作播报。

        **drop_pending=True**：本板 RTF ≈ 5，排队滞后的播报会说反话
        （车都已经停了，还在慢慢说"开始前进"），所以动作播报只留最新一条。
        """
        if not self._announce_actions:
            return False
        tpl = SPEAK_TEMPLATES.get(action)
        if not tpl:
            return False
        return self.speak(tpl, drop_pending=True)

    def move(self, action, speed=None, duration=None):
        """下发一个动作。

        action   : ACTION_XYZ 中的键
        speed    : mm/s，None 表示用当前速度
        duration : 秒；None 表示持续运动直到 stop()
        """
        if not self._connected or self._car is None:
            self.log("未连接串口，忽略动作：%s" % action)
            return False

        if action not in ACTION_XYZ:
            self.log("未知动作：%s" % action)
            return False

        # 🔒 急停锁定态下拒绝一切动作 —— 但 **stop 永远放行**，
        #    否则锁定之后连停车都做不了，那是更危险的设计。
        if self._estop_locked and action != "stop":
            self.log("急停锁定中，拒绝「%s」（先 release_estop 解除）"
                     % ACTION_LABEL.get(action, action))
            return False

        self._cancel_timer()

        if action == "stop":
            self._write_speed(0, 0, 0)
            self._action = "stop"
            self._announce(action)
            return True

        sp = _clamp_speed(speed if speed is not None else self._speed)
        self._speed = sp
        kx, ky, kz = ACTION_XYZ[action]
        try:
            self._write_speed(kx * sp, ky * sp, kz * sp)
        except Exception as e:
            self.log("下发指令异常：%s" % e)
            return False

        self._action = action
        if duration:
            self.log("%s  %d mm/s  %.2f s" % (ACTION_LABEL[action], sp, duration))
            self._schedule_stop(float(duration))
        else:
            self.log("%s  %d mm/s  持续" % (ACTION_LABEL[action], sp))
        self._announce(action)
        return True

    def stop(self):
        """停止（并取消定时停车）。"""
        if not self._connected or self._car is None:
            return False
        self._cancel_timer()
        try:
            self._write_speed(0, 0, 0)
        except Exception as e:
            self.log("停止指令异常：%s" % e)
            return False
        self._action = "stop"
        self._announce("stop")
        return True

    def emergency_stop(self):
        """
        急停：连发多次停止，尽量确保一定到位。

        ⚠️ 这是**纯粹的停车动作**，不带锁定、不带播报 ——
        完整的"急停语义"（停车 → 锁定 → 播报）在 `trigger_emergency()` 里。
        语音急停回调直接调 `trigger_emergency()`。
        这个方法特意保持最"薄"，因为它是安全链路上的最后一环。
        """
        if not self._connected or self._car is None:
            return False
        self._cancel_timer()
        self._action = "stop"
        for _ in range(5):
            try:
                self._write_speed(0, 0, 0)
            except Exception:
                pass
            time.sleep(0.02)
        self.log("★ 急停")
        return True

    def _schedule_stop(self, seconds):
        with self._timer_lock:
            t = threading.Timer(seconds, self._auto_stop)
            t.daemon = True
            self._timer = t
            t.start()

    def _cancel_timer(self):
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _auto_stop(self):
        with self._timer_lock:
            self._timer = None
        car = self._car
        if car is None:
            return
        try:
            self._write_speed(0, 0, 0)
            self._action = "stop"
            self.log("定时到，自动停止")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════
    #  本地 LLM（指令纠错 + 拟人化对话）
    # ══════════════════════════════════════════════════════════════════
    def init_llm(self, enabled=True, cli=None, model=None, timeout=60.0,
                 background=False, mode="auto", api_base=None,
                 api_key=None, api_model=None):
        """
        探测并启用 LLM（**本地或云端**）。

        `mode`：
          · `"auto"`（默认）—— 配了 API key 且网络通 → **云端**；否则**本地**。
            这样离线场景行为与以前完全一致，不会因为支持云端把离线弄坏。
          · `"api"`    —— 强制云端（智谱 GLM-4-Flash 等 OpenAI 兼容接口）。
          · `"server"` / `"cli"` —— 强制本地 llama.cpp。

        **分两步**，为了不冻界面：
          1. 同步探测（server 模式会真的启动服务并等就绪，实测本板约 4 秒）
             —— 很快，而且能把"没装模型/没配 key"立刻说清楚；
          2. `background=True` 时改成后台线程启动，界面显示"加载中"。

        ⚠️ **重复调用是安全的** —— 已启动的 server 不会被重启（重启会丢掉 KV 提示缓存，
        第一条查询又会从 ~15s 变回 ~45s）。

        返回 (是否可用, 提示)。
        """
        self._llm_enabled = bool(enabled)
        self._llm_mode = mode
        if not HAS_LLM:
            self._llm_state = "failed"
            self._llm_msg = "LLM 模块不可用：%s" % LLM_ERROR
            self.log(self._llm_msg)
            return False, self._llm_msg

        if self._llm is None:
            try:
                self._llm = _llm_engine.LlmEngine(
                    cli=cli, model=model, timeout=timeout, log=self.log,
                    mode=mode, api_base=api_base, api_key=api_key,
                    api_model=api_model)
            except Exception as e:
                self._llm_state = "failed"
                self._llm_msg = "LLM 初始化异常：%r" % (e,)
                self.log(self._llm_msg)
                return False, self._llm_msg

        # 已经就绪 → 直接返回（绝不重复启动 server）
        try:
            if self._llm.ready:
                self._llm_state = "ready"
                self._llm_msg = self._llm.msg
                return True, self._llm_msg
        except Exception:
            pass

        if background:
            self._llm_state = "loading"
            self._llm_msg = "正在启动本地 LLM…"

            def _work():
                try:
                    ok, msg = self._llm.load(background=False)
                except Exception as e:
                    ok, msg = False, "LLM 启动异常：%r" % (e,)
                self._llm_state = "ready" if ok else "failed"
                self._llm_msg = msg
                self.log("LLM：%s" % msg)
            threading.Thread(target=_work, daemon=True).start()
            return True, self._llm_msg

        try:
            ok, msg = self._llm.load(background=False)
        except Exception as e:
            ok, msg = False, "LLM 启动异常：%r" % (e,)
        self._llm_state = "ready" if ok else "failed"
        self._llm_msg = msg
        self.log("LLM：%s" % msg)
        return ok, msg

    def set_llm_enabled(self, on):
        """LLM 总开关。关掉后完全退回"规则没命中就说没听懂"的旧行为。"""
        self._llm_enabled = bool(on)
        self.log("本地 LLM 已%s" % ("启用" if self._llm_enabled else "关闭"))
        return self._llm_enabled

    def llm_status(self):
        st = {
            "state": self._llm_state,
            "msg": self._llm_msg,
            "enabled": self._llm_enabled,
            "mode_req": self._llm_mode,      # 请求的模式（auto/api/server/cli）
            "has_module": HAS_LLM,
            "replies": list(self._llm_replies),
        }
        if self._llm is not None:
            try:
                st.update(self._llm.status())
            except Exception as e:
                st["stats"] = {"error": repr(e)}
        return st

    def _llm_context(self):
        """给 LLM 的**当前状态**上下文。

        ★★ 设计要点（2026-09-30 修正，**踩过大坑**）：
          这里**绝不能放"当前动作"**（如 `动作左转`）！

        为什么（实测证据，务必保留这段结论）：
          提示词是拼成 `"<用户原话>\\n[当前状态]<上下文>"` 一起发出去的。
          一旦上下文里出现 `动作左转`，模型会把它当成**要执行的指令**，
          直接回吐 `{"cmd":"turn_left","say":"好的，正在左转。"}` ——
          哪怕用户说的是「你好。」。
          实测**三条路径全部中招**（function-calling / json_object / 纯提示词），
          且**100% 稳定复现**：
              说「你好。」+ [当前状态]…动作左转
              → {"cmd": "turn_left", "say": "好的，正在左转。"}
          而上一条若恰好是后退，就变成「正在后退」—— 用户实测报的就是这个：
          **不管说什么，车都回「正在后退」**（其实是上一条指令的动作被复读）。

        历史教训叠加：早期这里放"离线演示模式，不能真的动"，
        结果模型**因为这句而拒绝解析指令**（`钱进一米` 被答成"动不了呢"）。
        ⇒ **这个上下文要么留空，要么只说与"怎么回话"有关的中性事实**，
          凡是带"动作/权限/能不能"的语义都会被模型误用。

        现在只保留**连接状态**这一个中性事实。
        连"速度"也一起去掉了 —— 实测它同样会被复读：
            说「我还没连上小车呢」+ `当前速度300mm/s`
            → 「我已经连上小车了，现在速度是300mm/s」（复读速度，且没回答用户）
            去掉后 → 「已连接小车，请告诉我您的指令」 ✅
        而速度对"解析指令"毫无作用（`parse_text_command` 用的是 `self._speed`），
        所以去掉它零成本。
        """
        if self._connected and self._car is not None:
            return "已连接小车"
        # 未连接时**只说事实**，不说"不能动"（那是执行层的事，会让模型拒绝给 cmd）
        return "尚未连接小车"

    def llm_ready(self):
        """LLM 现在能用来推理吗？（模块在、加载成功、开关打开）"""
        if not (HAS_LLM and self._llm is not None and self._llm_enabled):
            return False
        # 后台启动可能在 init_llm(background=True) 之后才完成 → 顺手刷新一次状态
        if self._llm_state == "loading":
            try:
                if self._llm.ready:
                    self._llm_state = "ready"
                    self._llm_msg = self._llm.msg
                    self.log("LLM：%s" % self._llm_msg)
            except Exception:
                pass
        return self._llm_state == "ready"

    def _ask_llm_chat(self, text):
        """
        只闲聊地问一次（GUI 的「纯聊天」模式在**后台线程**里调它，
        再把结果交回界面线程播报）。
        """
        if self._llm is None or self._llm_state != "ready" or not self._llm_enabled:
            return ""
        try:
            r = self._llm.ask_chat(text, extra_context=self._llm_context())
        except Exception as e:
            self.log("LLM 聊天异常：%r" % (e,))
            return ""
        return (r.say or "").strip() if r is not None else ""

    def _ask_llm(self, text):
        """
        问 LLM 一句话。**永不抛异常、永远不会返回"要执行停车"**。

        返回 LlmReply 或 None（不可用/关闭时）。
        """
        if not self.llm_ready():
            return None
        try:
            r = self._llm.ask(text, extra_context=self._llm_context())
        except Exception as e:
            self.log("LLM 调用异常（已忽略，不影响控车）：%r" % (e,))
            return None
        if r is not None and not r.error:
            self._llm_replies.append((text, r.say or r.cmd or ""))
        return r

    def _llm_reply_to_result(self, reply):
        """
        把 LLM 答复转成 `run_text_command` 能用的指令 dict。

        ⚠️ 安全相关：
          · LLM 的动作白名单里**没有 stop**（见 llm_engine.ALLOWED_ACTIONS）；
          · 置信度低于 `LLM_MIN_CONFIDENCE` → **只当闲聊，不下发任何动作**；
          · 这里再判一次停车语义，命中就退回闲聊。
        """
        if reply is None:
            return None
        t = reply.say or ""
        if (reply.has_cmd
                and reply.confidence >= LLM_MIN_CONFIDENCE
                and not _llm_engine.looks_like_stop(t)):
            act = reply.cmd
            out = {"action": act}
            if act == "set_speed":
                out["value"] = reply.speed or self._speed
                return out
            if act in ("speed_up", "speed_down"):
                return out
            speed = reply.speed or self._speed
            out["speed"] = speed
            dur = reply.duration_s
            if dur is None and reply.distance_mm:
                dur = reply.distance_mm / max(speed, 1)
            if dur is not None:
                out["duration"] = round(min(max(dur, 0.1), 30.0), 2)
            return out
        return None

    def ask_chat(self, text, say=None):
        """
        **纯聊天**：只闲聊，绝不执行任何指令（GUI「纯聊天」模式）。

        实现方式是把用户的话原样交给 LLM 的"只许闲聊"入口
        （`allow_cmd=False, force_chat=True`），所以即使你说"前进一米"，
        它也只会回一句话 —— 演示时怕误动车就用这个。

        `say` 不为空时表示"回复已经算好了"（GUI 在后台线程算完再回到主线程播报）。
        """
        if say:
            self.log("聊天：%s" % say)
            if self._announce_actions:
                self.speak(say, drop_pending=True)
            self._llm_replies.append((text, say))
            return False, say
        if not self.llm_ready():
            return False, "（纯聊天模式，本地 LLM 未就绪）"
        reply = self._ask_llm_chat(text)
        if not reply:
            return False, "（没想好怎么回）"
        return self.ask_chat(text, say=reply)

    # ── 文本指令 ────────────────────────────────────────────────────
    def set_demo_mode(self, on):
        """
        离线（演示/对话）模式开关。

        **专为"没连小车也要能用"设计**：打开后，文本指令即使没有串口也照常被
        接受 —— 更新速度/动作等**本地状态**并**语音应答**，但**绝不发串口**。

        安全性：真正的下发口 `move()` / `stop()` 的"未连接直接拒绝"判断
        **没有改动**，所以这个开关**不可能让车动**。
        """
        self._demo = bool(on)
        self.log("离线演示模式已%s" % ("开启（不发串口，仅语音应答）" if self._demo else "关闭"))
        return self._demo

    def run_text_command(self, text, llm_hint=None):
        """
        解析并执行文本指令，返回 (是否执行, 提示信息)。

        流程（**顺序很重要**）：
          1. **规则快路径** `parse_text_command()` —— 命中就直接执行，**不经过 LLM**，
             保证常用指令零延迟、急停/停止永远由规则层负责；
          2. 规则没命中时才问**本地 LLM**（`_ask_llm`）：
             · 它给出高置信度指令 → 当纠错处理，执行（例："钱进一米"→前进一米）；
             · 否则 → 用它的 `say` 做**拟人化回复**；
          3. LLM 不可用 / 报错 / 超时 → **退回原来的"没听懂"**（行为与接入前一致）。

        `llm_hint`：**已经算好的** LLM 结果（GUI 在后台线程里先跑完推理，
        再把它带回来执行，避免 LLM 的 3~10 秒把界面冻住）。传了就不再自己调。

        三种连接状态下的落地方式：
          · 已连小车      → 正常下发；
          · 未连接 + 演示 → 更新本地状态 + 语音应答，**不发串口**；
          · 未连接 + 非演示 → 只提示"未连接"。
        """
        result, msg = parse_text_command(text, self._speed)

        # ── 规则没命中：交给 LLM 纠错 / 闲聊 ────────────────────────
        if result is None:
            reply = llm_hint if llm_hint is not None else self._ask_llm(text)
            if reply is not None and not reply.error:
                fixed = self._llm_reply_to_result(reply)
                if fixed is not None:
                    act = fixed["action"]
                    label = ACTION_LABEL.get(act, act)
                    self.log("LLM 纠错：「%s」→ %s" % (text, label))
                    # 纠正出来的指令走**原有的同一条执行路径**（含锁定/连接/演示判断）
                    ok, sub = self._do_action(fixed, ACTION_LABEL.get(act, ""),
                                              prefix="LLM纠错：")
                    # 纠正成功后补一句它的确认语（比"开始前进"更像在对话）
                    if ok and reply.say and self._announce_actions:
                        self.speak(reply.say, drop_pending=True)
                    return ok, sub
                # 纯闲聊：把它的回复说出来
                say = (reply.say or "").strip()
                if say:
                    self.log("LLM 回复：%s" % say)
                    if self._announce_actions:
                        self.speak(say, drop_pending=True)
                    return False, say
                self.log("LLM 没给出可用内容（raw=%s）" % (reply.raw or "")[:80])
            elif reply is not None and reply.error:
                self.log("LLM 不可用，退回规则提示：%s" % reply.error)

            # ── LLM 不可用/没内容：退回原来的行为 ──
            self.log("指令未识别：%s" % msg)
            # 听不懂时也出声，否则"没连小车"就完全是个哑巴，分不清
            # "没听懂"和"没反应"。关掉动作播报则不出声。
            if self._announce_actions:
                self.speak("没听懂", drop_pending=True)
            return False, msg

        # 规则命中 → 走统一执行入口（LLM 纠错出来的指令也走同一个函数）
        return self._do_action(result, msg)

    def _do_action(self, result, msg, prefix=""):
        """
        **统一的动作执行入口**（规则快路径与 LLM 纠错路径共用）。

        这样做的好处：锁定判断、连接判断、离线演示、播报这套逻辑只有一份，
        不会出现"某条新路径漏了安全检查"的问题（离线演示模式就踩过这个坑）。

        prefix: 日志前缀（LLM 纠错时传 "LLM纠错：" 以便区分来源）。
        """
        act = result.get("action")

        # ── 速度类：本来就是纯本地状态，不需要小车，也顺带补上语音应答 ──
        #   ⚠️ 播报前必须过 `speak_text()`：NPU TTS 读不出阿拉伯数字，
        #      "加速到313" 只会听到"加速到"就戛然而止（见 `_CN_DIGITS` 的说明）。
        if act == "set_speed":
            v = self.set_speed_value(result["value"])
            if self._announce_actions:
                self.speak(speak_text("速度%d%s" % (v, SPEED_UNIT_CN)), drop_pending=True)
            self.log("%s速度设为 %d mm/s" % (prefix, v))
            return True, "速度 %d" % v
        if act == "speed_up":
            v = self.speed_up()
            if self._announce_actions:
                self.speak(speak_text("加速到%d%s" % (v, SPEED_UNIT_CN)), drop_pending=True)
            self.log("%s加速 → %d mm/s" % (prefix, v))
            return True, "速度 %d" % v
        if act == "speed_down":
            v = self.speed_down()
            if self._announce_actions:
                self.speak(speak_text("减速到%d%s" % (v, SPEED_UNIT_CN)), drop_pending=True)
            self.log("%s减速 → %d mm/s" % (prefix, v))
            return True, "速度 %d" % v

        # ── 动作类：急停锁定是**最高优先级**，必须先于"未连接/演示"判断 ──
        #   ⚠️ 离线演示分支不走 move()，所以这里必须自己挡一次；
        #      否则会出现"锁定态下演示模式仍接受前进"这种安全漏洞（已用断言锁死）。
        #      与 move() 一致：**stop 永远放行**，其余一律拒绝。
        if self._estop_locked and act != "stop":
            self.log("急停锁定中，拒绝「%s」（先 release_estop 解除）"
                     % ACTION_LABEL.get(act, act))
            return False, "急停锁定中"

        if self._connected and self._car is not None:
            ok = self.move(act, result.get("speed"), result.get("duration"))
            return ok, msg

        # 未连接。stop 是安全动作，永远放行（只更新本地状态）
        if act == "stop":
            self._action = "stop"
            if self._demo:
                self.log("[离线] 停止 — 未连小车")
                if self._announce_actions:
                    tpl = SPEAK_TEMPLATES.get("stop")
                    if tpl:
                        self.speak(tpl, drop_pending=True)
                return True, "离线：%s" % msg
            self.log("未连接串口，忽略动作：stop")
            return False, "未连接小车：停止"

        # 未连接 + 非演示：静默拒绝（原行为 —— 只记日志，不播报废话）
        if not self._demo:
            self.log("未连接串口，忽略动作：%s" % act)
            return False, "未连接小车：%s" % msg

        # 离线演示：更新本地状态 + 语音应答，绝不碰串口
        sp = _clamp_speed(result.get("speed") or self._speed)
        self._speed = sp
        self._action = act
        dur = result.get("duration")
        self.log("%s[离线] %s  %d mm/s%s — 未连小车，仅语音应答"
                 % (prefix, ACTION_LABEL.get(act, act), sp,
                    ("  %.2f s" % dur) if dur else "  持续"))
        if self._announce_actions:
            tpl = SPEAK_TEMPLATES.get(act)
            if tpl:
                # 只留最新一条：本板 RTF≈5，排队滞后的播报会说反话
                self.speak(tpl, drop_pending=True)
        return True, "离线：%s" % msg

    # ══════════════════════════════════════════════════════════════════
    #  语音播报（TTS）
    # ══════════════════════════════════════════════════════════════════
    def init_tts(self, speaker=None, enabled=True, background=True,
                 device=None, sink=None, web_port=None):
        """
        预加载 TTS 引擎并起播报队列。

        `web_port` 控制**网络音箱**：
          · `None`（默认）= 自动 —— 本机没有本地播放设备时，自动在 8099
            起一个网络音箱，把播报送到浏览器（本板正是这种情况）；
          · `0` = 不起；
          · 具体端口号 = 用它。
        有本地喇叭时不需要它，所以"自动"不会白起服务。

        ⚠️ **默认异步加载**：模型加载要 5~7 秒，同步做会把界面整整冻死 7 秒。
        进度看 `tts_status()`；`_tts_state` 走 off → loading → ready / failed。

        为什么要"启动时预加载"：加载 7 秒 + 单句合成约 6 秒 ——
        如果等要播报了才加载，第一次播报要等 13 秒，急停场景完全不可接受。

        返回 (是否已开始, 提示)。
        """
        if not HAS_TTS:
            self._tts_state = "failed"
            self._tts_msg = "缺少 tts_engine 模块：%s" % TTS_ERROR
            self.log("TTS 不可用：%s" % self._tts_msg)
            return False, self._tts_msg
        if self._tts_state == "ready":
            return True, "TTS 已就绪"
        if self._tts_state == "loading":
            return True, "TTS 正在加载中"

        self._tts_speaker = speaker
        self._speak_enabled = bool(enabled)

        def _worker():
            eng = _tts_engine.TtsEngine(
                log=lambda s: self.log("[TTS] %s" % s))
            # ★ 优先 NPU 后端（FastSpeech2 的 5 个 om，实测 RTF 0.92、
            #   比 Kokoro 快 5~10 倍）；**不可用就自动回退 sherpa/CPU**，
            #   行为与以前完全一致 —— NPU 是加速项，不是依赖。
            backend, ok, msg = eng.load_best(prefer_npu=True, num_threads=2)
            if not ok:
                self._tts_state = "failed"
                self._tts_msg = msg
                self.log("TTS 加载失败：%s" % msg)
                return
            self.log("TTS 后端：%s（%s）" % (backend.upper(), msg))
            spk = speaker if speaker is not None \
                else _tts_engine.default_speaker(eng._dir)

            # 🔴 关键一步：本板**没有本地播放设备**。如果不做这一步，
            #    播报合成完就丢在本地，接进小车也永远听不到。
            #    这里把播报接上"网络音箱"，浏览器/手机就能当车的喇叭。
            has_local = bool(_tts_engine.output_probe()["ok"])
            port = 8099 if web_port is None else int(web_port)
            if not has_local and port:
                try:
                    import tts_serve as _ts
                    srv, bc, err = _ts.start_background(
                        eng, port=port, log=lambda s: self.log(s))
                    if err:
                        self.log("网络音箱启动失败：%s" % err)
                    else:
                        self._web = (srv, bc, port)
                except Exception as e:
                    self.log("网络音箱异常：%s" % e)

            try:
                q = _tts_engine.SpeakQueue(
                    eng, speaker=spk, device=device, sink=sink,
                    on_done=self._on_spoken,
                    # ★ 播报前后让 KWS 让出麦克风（本板采集/播放双向互斥，
                    #   KWS 常驻占着麦克风会让 aplay 必然失败）
                    mic_pause=self._pause_kws_for_playback,
                    mic_resume=self._resume_kws_after_playback)
            except Exception as e:
                self._tts_state = "failed"
                self._tts_msg = "起播报队列失败：%s" % e
                self.log(self._tts_msg)
                return
            # ⚠️ 先装好队列、再置 ready。反了会留下一个
            #    "状态是 ready 但 _speak_q 还是 None" 的窗口，播报会静默丢失。
            self._tts = eng
            self._speak_q = q
            self._tts_state = "ready"
            self._tts_backend = backend
            if has_local:
                out_desc = "板载喇叭"
            elif self._web:
                out_desc = "网络音箱 " + self.web_url()
            else:
                out_desc = "无（没喇叭，网络音箱也没起来）"
            # 防御：走 NPU 后端时 TtsEngine.load() 没被调用，_dir 可能为 None
            _mdlname = os.path.basename(eng._dir) if eng._dir else (
                "FastSpeech2-NPU" if backend == "npu" else "?")
            self._tts_msg = "就绪（%s，说话人 %d，后端 %s，输出：%s）" % (
                _mdlname, spk, backend.upper(), out_desc)
            self.log("TTS %s" % self._tts_msg)
            # 不再额外发 tts_ready 事件：上面这行日志 + tts_status() 已经够了，
            # 再发一次会让界面日志出现两条一样的"TTS 就绪"。
            if self._speak_enabled:
                self.speak(SPEAK_READY, force=True)

        self._tts_state = "loading"
        self._tts_msg = "正在加载模型…"
        self.log("正在加载 TTS 模型（约 5~7 秒，期间不影响手动控制）…")
        if background:
            threading.Thread(target=_worker, daemon=True,
                             name="tts-loader").start()
            return True, "TTS 后台加载中"
        _worker()
        return self._tts_state == "ready", self._tts_msg

    def _on_spoken(self, ok, msg, text):
        """播报完成回调（在播报线程里执行，只记日志，不碰界面）。"""
        if not ok:
            self.log("播报失败「%s」：%s"
                     % (text, (msg or "").splitlines()[0][:80]))

    def web_url(self):
        """网络音箱的收听地址（没起则空串）。"""
        if not self._web:
            return ""
        try:
            import tts_serve as _ts
            return "http://%s:%d/listen" % (_ts.local_ip(), self._web[2])
        except Exception:
            return ""

    def tts_status(self):
        """TTS 状态快照（界面显示用）。"""
        has_out = False
        if HAS_TTS and self._tts_state == "ready":
            try:
                has_out = bool(_tts_engine.output_probe()["ok"])
            except Exception:
                has_out = False
        q = self._speak_q
        return {
            "state": self._tts_state,
            "msg": self._tts_msg,
            "backend": self._tts_backend,     # "npu" / "cpu" / ""
            "speaker": self._tts_speaker,
            "enabled": self._speak_enabled,
            "announce_actions": self._announce_actions,
            "pending": (q.pending() if q else 0),
            "speaking": bool(q is not None and getattr(q, "speaking", False)),
            "has_output": has_out,          # 有没有**本地**喇叭
            "web_url": self.web_url(),      # 网络音箱地址（板子没喇叭时用它）
        }

    def speak(self, text, drop_pending=False, force=False):
        """
        投递一句播报。**立即返回，绝不等待合成。**

        返回是否真的投递进去。**未就绪 / 关掉 / 冷却中一律返回 False ——
        调用方必须把它当成"允许失败"**，绝不能因为没声音影响车的动作。

        drop_pending: 清掉排队中的旧播报（动作播报"过期作废"、急停抢占）
        force:        绕过播报开关与冷却（系统级提示用）
        """
        text = (text or "").strip()
        if not text:
            return False
        if not self._speak_enabled and not force:
            return False
        q = self._speak_q
        if q is None:
            return False                    # 还没加载好 / 加载失败

        now = time.time()
        if not force:
            last = self._last_speak.get(text, 0.0)
            if (now - last) < SPEAK_COOLDOWN:
                return False                # 冷却：同一条短时间内不重复
        self._last_speak[text] = now
        try:
            q.put(text, drop_pending=drop_pending)
        except Exception as e:
            self.log("播报投递异常：%s" % e)
            return False
        return True

    def set_speak_enabled(self, on):
        """播报总开关。关掉不影响急停播报（那条走 force）。"""
        self._speak_enabled = bool(on)
        self.log("语音播报已%s" % ("开启" if self._speak_enabled else "关闭"))
        return self._speak_enabled

    def set_announce_actions(self, on):
        """是否播报动作确认（"开始前进"这类）。嫌吵就关掉。"""
        self._announce_actions = bool(on)
        self.log("动作播报已%s" % ("开启" if self._announce_actions else "关闭"))
        return self._announce_actions

    def shutdown_tts(self):
        """停止播报队列与网络音箱（退出程序时调用）。"""
        q = self._speak_q
        self._speak_q = None
        w = self._web
        self._web = None
        self._tts = None
        self._tts_state = "off"
        if q is not None:
            try:
                q.close()
            except Exception:
                pass
        if w is not None:
            try:
                w[0].shutdown()
                w[0].server_close()
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════
    #  语音急停（KWS）+ 急停锁定态
    # ══════════════════════════════════════════════════════════════════
    def trigger_emergency(self, reason="手动", announce=True):
        """
        急停的**完整语义**，顺序不可颠倒：

            ① 立即下发零速度帧     ← 同步、最高优先级、不排队
            ② 进入急停锁定态       ← 之后必须人工解除
            ③ 【之后】才异步播报"已急停"

        ⚠️ **绝不能先播报再停车**。合成要好几秒，那几秒里车还在跑 ——
        这是安全问题，不是性能问题。所以第 ③ 步是"投递"，不是"等待"。

        返回 §① 是否成功（下位机是否收到停车帧）。
        """
        ok = self.emergency_stop()              # ①
        self._estop_locked = True               # ②
        self._estop_reason = reason
        self.log("🔒 进入急停锁定态（%s）—— 需人工解除" % reason)
        self._voice_events.append(("estop", reason, time.time()))
        if announce:
            self.speak(SPEAK_ESTOP, drop_pending=True, force=True)   # ③
        return ok

    @property
    def estop_locked(self):
        return self._estop_locked

    @property
    def estop_reason(self):
        return self._estop_reason

    def release_estop(self, announce=True):
        """
        人工解除急停锁定。返回被解除的原因（本来没锁返回空串）。

        **刻意做成必须人工调用**：误触发之后车不会自己又跑起来。
        """
        if not self._estop_locked:
            return ""
        reason = self._estop_reason
        self._estop_locked = False
        self._estop_reason = ""
        self.log("🔓 已解除急停锁定（原由：%s）" % reason)
        self._voice_events.append(("estop_released", reason, time.time()))
        if announce:
            self.speak(SPEAK_RELEASED, drop_pending=True, force=True)
        return reason

    def enable_voice_emergency(self, keywords_file=None, threshold=None,
                              cooldown=None, device=None, background=True):
        """
        开启**语音急停**：KWS 常开监听，命中就 `trigger_emergency()`。

        这是安全能力，**应当常开**；配套的"播报"只是附加提示。

        `threshold=None` 表示**用 kws_engine 的默认阈值**。注意不要把它
        直接透传成显式 None —— 那会覆盖被调用方的默认值（2026-09-29 踩过，
        导致语音急停必然启动失败）。这里统一解析好再传。

        返回 (是否已开始, 提示)。
        """
        # 把 None 解析成真正的默认值（而不是把 None 传下去）
        if threshold is None:
            threshold = getattr(_kws_engine, "DEFAULT_THRESHOLD", None)
        if not HAS_KWS:
            self._kws_state = "failed"
            self._kws_msg = "缺少 kws_engine 模块：%s" % KWS_ERROR
            self.log("语音急停不可用：%s" % self._kws_msg)
            return False, self._kws_msg
        if self._kws_state in ("ready", "loading"):
            return True, "语音急停已在运行"

        self._kws_state = "loading"
        self._kws_msg = "正在加载 KWS…"

        def _worker():
            try:
                eng = _kws_engine.KwsEngine(
                    log=lambda s: self.log("[KWS] %s" % s))
                ok, msg = eng.load(keywords_file=keywords_file,
                                   threshold=threshold)
                if not ok:
                    self._kws_state = "failed"
                    self._kws_msg = msg
                    self.log("KWS 加载失败：%s" % msg)
                    return
                extra = {"cooldown": cooldown} if cooldown is not None else {}
                lis = _kws_engine.KwsListener(
                    engine=eng, device=device, on_hit=self._on_kws_hit,
                    log=lambda s: self.log("[KWS] %s" % s), **extra)
                ok, msg = lis.start()
                if not ok:
                    self._kws_state = "failed"
                    self._kws_msg = msg
                    self.log("语音急停启动失败：%s" % msg)
                    return
                self._kws = lis
                self._kws_state = "ready"
                self._kws_msg = "监听中（阈值 %s）" % (
                    threshold if threshold is not None else "默认")
                self.log("🔴 语音急停已开启：%s" % self._kws_msg)
                self._voice_events.append(
                    ("kws_ready", self._kws_msg, time.time()))
            except Exception as e:
                self._kws_state = "failed"
                self._kws_msg = "语音急停启动异常：%s" % e
                self.log(self._kws_msg)

        if background:
            threading.Thread(target=_worker, daemon=True,
                             name="kws-loader").start()
            return True, "语音急停后台启动中"
        _worker()
        return self._kws_state == "ready", self._kws_msg

    def _on_kws_hit(self, label, ts):
        """
        KWS 命中回调 —— **运行在监听线程里**，必须快，且绝不能碰界面控件。

        这里只做三件事：记一笔、立即急停、投递播报。
        真正的合成与播放由 SpeakQueue 的后台线程慢慢做，这里投完就返回。
        """
        self._kws_hits.append((label, ts))
        self.log("★ 语音命中「%s」→ 立即急停" % label)
        self.trigger_emergency(reason="语音命中「%s」" % label)

    def disable_voice_emergency(self):
        lis = self._kws
        self._kws = None
        self._kws_state = "off"
        self._kws_msg = ""
        if lis is not None:
            try:
                lis.stop()
            except Exception:
                pass
            self.log("语音急停已关闭")

    # ══════════════════════════════════════════════════════════════════
    #  麦克风租借（mic lease）—— ★ 2026-09-29 新增，修"打开麦克风失败"
    # ══════════════════════════════════════════════════════════════════
    #
    #  问题现场：界面频繁报
    #     "打开麦克风失败（设备 0）：Unanticipated host error -9999:
    #      'Input/output error' [ALSA error -5]"
    #  实测根因（板上复现，成功率 0/5）：
    #     **本板 USB 声卡的 capture 只有 1 个 subdevice**（`arecord -l` 显示
    #     `Subdevices: 0/1` 就表示被占满）→ **硬件级互斥，同一时刻只能有一路采集**。
    #     而「开始聆听 / 语音急停」起的是**常驻 InputStream**，它一直占着麦克风；
    #     此时再点「按住说话」，第二路必然失败。
    #
    #  解法：**谁要用麦克风，谁先把它要过来**（租借）。
    #     录音前 `_yield_mic()` 停掉 KWS 监听并**等它真正释放**，
    #     录完 `_restore_mic()` 再把它起回来。
    #
    #  ⚠️ 安全权衡（重要）：让位期间**语音急停短暂失效**，所以
    #     · 只在"用户明确在录音"这种主动操作时让位，且录完立刻恢复；
    #     · 每次都写日志告知用户，不静默降级；
    #     · 急停被锁定时**不让位**（那时车已停，以保持急停链路可用为先）。
    # ── 播报让位（录音前用）────────────────────────────────────────
    #: 播放设备的 ALSA 状态文件（用来判断"声卡此刻是否真在响"）
    @staticmethod
    def _playback_busy():
        """声卡**此刻是否真的正在播放**（跨进程检测）。

        ⚠️ **为什么光看播报队列不够**（2026-09-30 确定性测试暴露）：
        本板播报是**起一个 `aplay` 子进程**放 wav 的。队列说"这一句说完了"，
        但那个 `aplay` 可能**还在响**（甚至别的进程/调试工具占着声卡）。
        实测：外部 `aplay` 稳定占住扬声器时 `speak_idle()` 返回 True（误判），
        于是麦克风照样打不开 —— 修复等于没生效。

        ⇒ 直接问 ALSA：`/proc/asound/card*/pcm*p/sub0/status` 里
          `state: RUNNING` 就是正在播放。这是**内核给的真相**，
          不管是谁在放（本进程、aplay、还是别的程序）都能看出来。
        """
        import glob as _glob
        try:
            for p in _glob.glob("/proc/asound/card*/pcm*p/sub*/status"):
                with open(p) as f:
                    if "state: RUNNING" in f.read():
                        return True
        except Exception:
            return False
        return False

    def speak_idle(self):
        """**扬声器**是否彻底安静：播报队列空 + 没在合成 + 声卡真的没在响。"""
        q = self._speak_q
        if q is not None:
            try:
                if not q.idle():
                    return False
            except Exception:
                pass
        # ★ 还要确认声卡不在响（子进程 aplay 可能还在跑，见 _playback_busy）
        return not self._playback_busy()

    def playback_idle(self):
        """**只问声卡**是否安静，不看合成队列。

        🔴 为什么需要它（2026-10-11 用户反馈"每说一句要等老长时间"）：
        本板互斥的是**播放 vs 录音**（同一 xHCI 控制器），
        而**合成只吃 CPU、完全不碰音频设备**。
        所以"能不能开麦克风"只取决于 `_playback_busy()`，
        与"是否还在合成下一句"无关 —— 用 `speak_idle()` 会把那段
        **~1.11s 的合成本该省掉的时间**也算进等待里。
        """
        return not self._playback_busy()

    def yield_speaker(self, max_wait=3.0):
        """录音前把**扬声器**让出来。返回 (是否已安静, 说明)。

        🔴 **为什么必须做**（2026-09-30 用户实测报的 bug）：
        本板两块 USB 音频设备（播放 card1「SPACETOUCH USB Audio」、
        采集 card0「USB PnP Sound Device」）**挂在同一个 xHCI 控制器上**，
        实测冲突矩阵：
            无播放      → 麦克风 ✅
            **播放中**  → 麦克风 ❌ `-9999 Input/output error`
            播放停止后  → 麦克风 ✅
        也就是说**只要在播报，麦克风就完全打不开**。

        ★★ 2026-10-11 两次提速（用户反馈"等老长时间才能再输入"）：
          ① **判据从 `speak_idle()` 改成 `playback_idle()`** ——
             只等声卡不再出声，**不等合成结束**。合成不碰音频设备，
             而它要 ~1.11s/句，原来这段干等是纯浪费。
             配合"合成后再查一次抢占"（见 `npu_tts_engine.say`），
             正在合成的那一句会被**直接丢弃、绝不播出**，所以开麦是安全的。
          ② **宽限期 0.35s → 0.12s**，轮询 25ms → 20ms。
             用户按 F2 的语义就是"我要说话了"，早一点掐断更符合直觉。
        """
        q = self._speak_q
        dropped = 0
        if q is not None:
            try:
                # ★ 抢占：丢掉排队的，并让正在说的那句**说完当前分句就停**。
                #   否则用户按 F2 后要干等"后面几句"的合成+播放。
                dropped = q.preempt()
            except Exception:
                try:
                    dropped = q.clear_pending()
                except Exception:
                    dropped = 0

        # ★ 快路径：声卡已经不出声 → 立刻返回，**不等合成**
        #   ⚠️ 也不能只确认一次：aplay 被 kill 后 ALSA 状态在 teardown
        #      的一瞬间可能仍报 RUNNING（反之也可能短暂报空闲）。
        #      实测出现过"让位后 0.5s 又漏播一帧"。所以统一走下面的
        #      **去抖**逻辑：连续两次读到安静才算数（只多花 20ms）。
        t0 = time.time()
        grace = 0.12
        killed = False
        confirm = 0
        while time.time() - t0 < max_wait:
            if self.playback_idle():
                confirm += 1
                if confirm >= 2:      # 连续两次（间隔 ~20ms）确认真的安静
                    if dropped:
                        return True, "已抢占播报（丢弃 %d 条，%.3fs）" % (
                            dropped, time.time() - t0)
                    return True, "已让出扬声器（%.3fs）" % (time.time() - t0)
            else:
                confirm = 0
                if not killed and (time.time() - t0) >= grace:
                    killed = self._stop_playback()
            time.sleep(0.02)
        return False, "播报仍在进行（已等 %.1fs）" % (time.time() - t0)

    def _stop_playback(self):
        """掐掉正在播放的音频（子进程）。返回是否真的掐了。"""
        eng = self._tts
        if eng is None:
            return False
        try:
            fn = getattr(eng, "stop_playback", None)
            return bool(fn()) if fn else False
        except Exception as e:
            self.log("打断播报异常（已忽略）：%r" % (e,))
            return False

    def _yield_mic(self):
        """把麦克风从 KWS 监听手里要过来。返回 (是否有让位, 说明)。

        没有在监听时返回 (False, "") —— 调用方据此决定要不要恢复。
        """
        lis = self._kws
        if lis is None:
            return False, ""
        if getattr(self, "_estop_locked", False):
            return False, "急停已锁定，保持语音急停可用（不让位）"
        try:
            if not getattr(lis, "running", False):
                return False, ""
        except Exception:
            pass
        try:
            lis.stop()
        except Exception as e:
            self.log("录音前停止语音急停异常：%r" % (e,))
            return False, ""
        self._kws_yielded = True
        self._mic_owned_by_record = True      # ★ 标记：录音拿着麦克风
        # ★ 关键：ALSA 释放不是瞬间的。实测 stop() 后立刻开录仍可能撞 -9999，
        #   所以这里主动等一小会儿（加上 Recorder 自己的退避重试，双保险）。
        time.sleep(0.35)
        self.log("🎤 录音需要麦克风，语音急停已临时让位（录完自动恢复）")
        return True, "语音急停已临时让位"

    def _pause_kws_for_playback(self):
        """播报前**让 KWS 让出麦克风**。返回是否真的暂停了。

        🔴 为什么必须做（2026-10-11 用户报「播报又一直失败」）：
        本板采集与播放是**双向互斥**的（同一 xHCI + 全速 USB 带宽不足）。
        实测：
            · 麦克风空闲        → `aplay -D plughw:1,0` rc=0 ✅ 正常播 2.0s
            · 麦克风被常驻占着  → `aplay: set_params:1416:
                                   Unable to install hw params` ❌ **必失败**
        而 KWS 是**常驻监听**，一直占着麦克风 ⇒
        「急停活着」和「能播报」互相掐死（这正是用户先看到"急停失效"、
        修好后又看到"播报一直失败"的原因 —— 同一枚硬币的两面）。

        ⇒ 播报前 `pause()`（KWS 关流让位），播报后 `resume()`（自动重开）。
        **对急停零损失**：播报期间麦克风本来也打不开，暂停不丢任何监听能力。
        """
        lis = self._kws
        if lis is None:
            return False
        if self._mic_owned_by_record:
            return False          # 录音正拿着麦克风：播报本来也播不出来，别乱动
        try:
            if not getattr(lis, "running", False):
                return False
            return bool(lis.pause())
        except Exception as e:
            self.log("暂停 KWS 失败（忽略）：%r" % (e,))
            return False

    def _resume_kws_after_playback(self):
        """播报后恢复 KWS 监听（它会自动重新打开麦克风）。"""
        lis = self._kws
        if lis is None:
            return
        # ★ 录音正在用麦克风（用户在播报中途按了 F2 插话）→ 别抢回来，
        #   交给 PTT 那条路的 `_restore_mic()` 自己恢复
        if self._mic_owned_by_record:
            return
        try:
            # ★ 先等扬声器彻底排空再开麦（2026-10-11）：
            #   否则麦克风会收到自己的余音，若刚播的那句含
            #   「已急停/停车/刹车」就会**自我触发急停**
            #   （实测播报「停车」后 1 秒内命中「急停」，车被自己的声音锁死）。
            #   KWS 侧还有 `ECHO_GUARD_SECONDS` 二次兜底。
            time.sleep(0.25)
            if lis.resume():
                self.log("🎤 播报结束，语音急停已恢复监听")
        except Exception as e:
            self.log("恢复 KWS 失败（忽略）：%r" % (e,))

    def _restore_mic(self, yielded):
        """录音结束后把麦克风还给 KWS 监听。"""
        if not yielded or not getattr(self, "_kws_yielded", False):
            return
        self._kws_yielded = False
        self._mic_owned_by_record = False     # ★ 录音交还麦克风
        lis = self._kws
        if lis is None:
            return
        try:
            ok, msg = lis.start()
            if ok:
                self.log("🎤 语音急停已恢复：%s" % msg)
            else:
                self._kws_state = "failed"
                self._kws_msg = msg
                self.log("⚠ 语音急停恢复失败：%s（请重新点「开启语音急停」）" % msg)
        except Exception as e:
            self._kws_state = "failed"
            self._kws_msg = "恢复异常：%r" % (e,)
            self.log("⚠ 语音急停恢复异常：%r" % (e,))

    def voice_status(self):
        """语音急停状态快照（界面显示 + 断链告警用）。"""
        lis = self._kws
        wd = None
        if lis is not None:
            try:
                wd = lis.watchdog_ok()
            except Exception:
                wd = None
        return {
            "state": self._kws_state,
            "msg": self._kws_msg,
            "watchdog_ok": wd,      # False = 麦克风链路死了，界面要红字报警
            "running": bool(lis is not None and getattr(lis, "running", False)),
            #: ★ 是否正在**尝试打开麦克风**（多半是被播报占着，一两秒后自恢复）。
            #:   界面据此显示"正在恢复监听…"，而不是谎报绿色"监听中"。
            "opening": bool(lis is not None
                            and getattr(lis, "_opening", False)),
            "level": (getattr(lis, "level", 0.0) if lis is not None else 0.0),
            "hits": list(self._kws_hits)[-10:],
            "locked": self._estop_locked,
            "lock_reason": self._estop_reason,
        }

    def poll_voice_events(self):
        """
        取出并清空语音事件（界面定时器调用）。

        事件形如 `("estop", 原因, 时刻)` / `("kws_ready", ...)`
        / `("tts_ready", ...)`，界面据此弹提示、闪红灯。
        """
        out = list(self._voice_events)
        self._voice_events.clear()
        return out

    def shutdown_voice(self):
        """关闭语音急停 + 播报队列 + 本地 LLM（退出程序时调用）。"""
        self.disable_voice_emergency()
        self.shutdown_tts()
        self.shutdown_llm()

    def shutdown_llm(self):
        """停掉常驻 llama-server。**必须调**，否则会留下占内存的子进程。"""
        llm = self._llm
        self._llm = None
        if llm is not None:
            try:
                llm.close()
                self.log("本地 LLM 已停止")
            except Exception as e:
                self.log("停止 LLM 异常：%r" % (e,))
        self._llm_state = "off"

    # ── 状态 ────────────────────────────────────────────────────────
    def status(self):
        """返回一份状态快照 dict（界面定时器调用）。"""
        snap = {
            "connected": self._connected,
            "port": self._port,
            "speed": self._speed,
            "action": self._action,
            "action_label": ACTION_LABEL.get(self._action, "-"),
            "have_feedback": False,
            "stale": False,
            "flag_stop": 0,
            "vx": 0, "vy": 0, "vz": 0,
            "voltage": 0.0,
            "accel": (0, 0, 0),
            "gyro": (0, 0, 0),
            "accel_ms2": (0.0, 0.0, 0.0),
            "gyro_rads": (0.0, 0.0, 0.0),
            "volt_level": "unknown",   # ok / warn / danger
            # ── 语音 / 安全状态 ──
            # 放在这里（而不是下面）是因为它们**与串口无关**：
            # 没连串口时界面也要能显示 TTS 加载进度和急停锁定态。
            "estop_locked": self._estop_locked,
            "estop_reason": self._estop_reason,
            "demo_mode": self._demo,
            "speak_enabled": self._speak_enabled,
            "tts_state": self._tts_state,
            "tts_msg": self._tts_msg,
            "speak_pending": (self._speak_q.pending() if self._speak_q else 0),
            "kws_state": self._kws_state,
            "kws_msg": self._kws_msg,
            "kws_watchdog_ok": None,
            "voice_hit_count": len(self._kws_hits),
            # ── 本地 LLM（与串口无关，同样放在早退之前）──
            "llm_state": self._llm_state,
            "llm_msg": self._llm_msg,
            "llm_enabled": self._llm_enabled,
            "llm_reply_count": len(self._llm_replies),
        }
        lis = self._kws
        if lis is not None:
            try:
                snap["kws_watchdog_ok"] = lis.watchdog_ok()
            except Exception:
                snap["kws_watchdog_ok"] = None

        car = self._car
        if car is None:
            return snap

        fb = None
        try:
            fb = car.get_feedback()
        except Exception:
            fb = None

        if fb:
            self._last_feedback_time = time.time()
            snap.update({
                "have_feedback": True,
                "flag_stop": fb.get("flag_stop", 0),
                "vx": fb.get("vx", 0),
                "vy": fb.get("vy", 0),
                "vz": fb.get("vz", 0),
                "voltage": fb.get("voltage", 0.0),
                "accel": (fb.get("accel_x", 0), fb.get("accel_y", 0), fb.get("accel_z", 0)),
                "gyro": (fb.get("gyro_x", 0), fb.get("gyro_y", 0), fb.get("gyro_z", 0)),
            })
            ax, ay, az = snap["accel"]
            gx, gy, gz = snap["gyro"]
            snap["accel_ms2"] = (ax / 1671.84, ay / 1671.84, az / 1671.84)
            snap["gyro_rads"] = (gx * 0.00026644, gy * 0.00026644, gz * 0.00026644)

            v = snap["voltage"]
            if v <= 0:
                snap["volt_level"] = "unknown"
            elif v < VOLT_LOCK:
                snap["volt_level"] = "danger"
            elif v < VOLT_WARN:
                snap["volt_level"] = "warn"
            else:
                snap["volt_level"] = "ok"
        else:
            # 连上了但超过 2 秒没有回传，说明下位机可能没在发数据
            if self._last_feedback_time and (time.time() - self._last_feedback_time) > 2.0:
                snap["stale"] = True

        return snap


# ══════════════════════════════════════════════════════════════════════
#  命令行自测
# ══════════════════════════════════════════════════════════════════════
def _main():
    import argparse

    ap = argparse.ArgumentParser(description="小车 GUI 核心逻辑自测")
    ap.add_argument("--port", default=None, help="串口设备名，默认自动探测")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--seconds", type=float, default=5.0, help="监听状态时长")
    ap.add_argument("--demo", action="store_true", help="跑一遍前进/后退/转向")
    ap.add_argument("--no-connect", action="store_true",
                    help="不连串口（只测语音链路，无需硬件）")
    ap.add_argument("--tts", action="store_true", help="启动时预加载 TTS 播报")
    ap.add_argument("--speaker", type=int, default=None,
                    help="TTS 说话人编号（默认用模型推荐值，Kokoro=47）")
    ap.add_argument("--say", default=None,
                    help="合成并播报指定文本后退出（**不需要小车硬件**）")
    ap.add_argument("--voice", action="store_true",
                    help="开启语音急停（KWS 常开监听，命中即急停+播报）")
    args = ap.parse_args()

    print("=" * 56)
    print(" %s  核心自测" % APP_NAME)
    print("=" * 56)
    print("pyserial     :", "OK" if HAS_PYSERIAL else "缺失")
    print("car_serial   :", "OK" if HAS_CAR_MODULE else ("缺失 " + CAR_MODULE_ERROR))
    print("TTS(语音播报) :", "OK" if HAS_TTS else ("缺失 " + TTS_ERROR))
    print("KWS(语音急停) :", "OK" if HAS_KWS else ("缺失 " + KWS_ERROR))
    print("可用串口     :", list_serial_ports() or "（无）")
    print("建议串口     :", guess_default_port())

    # ── 只播报一句就退出：**不需要硬件**，用来验证 TTS 链路是否通 ──
    if args.say is not None:
        ctrl0 = CarController()
        ok, msg = ctrl0.init_tts(speaker=args.speaker, background=False)
        print("\nTTS 加载：%s，%s" % (ok, msg))
        if not ok:
            print("  → 先跑：bash run_py.sh tts_engine.py --check")
            return 1
        print("  %s" % ctrl0.tts_status()["msg"])
        q = ctrl0._speak_q
        try:
            if not ctrl0.speak(args.say, force=True):
                print("播报投递失败（未就绪？）")
                return 1
            print("已投递「%s」，等它说完…" % args.say)
            t0 = time.time()
            while not q.idle() and time.time() - t0 < 90:
                time.sleep(0.2)
            print("完成（%.1f 秒）" % (time.time() - t0))
        finally:
            ctrl0.shutdown_tts()
        return 0

    # 顺便测一下文本解析，不需要硬件
    print("\n-- 文本指令解析自测 --")
    for s in ("前进", "前进一米", "左转90度", "后退 2 秒", "速度 200",
              "快一点", "停", "急停", "你好", "给我讲个笑话"):
        r, m = parse_text_command(s)
        print("  %-10s -> %s" % (s, r if r else ("未识别：" + m)))

    ctrl = CarController()

    # TTS 预加载放在连串口**之前** —— 加载要 5~7 秒，
    # 让它和"接线 / 等下位机"的时间重叠，省一次白等。
    if args.tts or args.voice:
        ok, msg = ctrl.init_tts(speaker=args.speaker)
        print("\nTTS：%s" % msg)

    if args.no_connect:
        print("\n-- 跳过串口连接（--no-connect，只测语音链路）--")
    else:
        ok, msg = ctrl.connect(args.port, args.baud)
        print("\n%s：%s" % ("连接成功" if ok else "连接失败", msg))
        if not ok:
            ctrl.shutdown_voice()
            return 1

    if args.voice:
        ok, msg = ctrl.enable_voice_emergency()
        print("语音急停：%s" % msg)

    try:
        if args.demo and ctrl.connected:
            print("\n-- 运动演示 --")
            for act, dur in (("forward", 1.5), ("stop", 0.3), ("backward", 1.5),
                             ("stop", 0.3), ("turn_left", 1.0), ("stop", 0.3),
                             ("turn_right", 1.0)):
                if act == "stop":
                    ctrl.stop()
                    time.sleep(dur)
                else:
                    ctrl.move(act, 200, dur)
                    time.sleep(dur + 0.3)
            ctrl.stop()

        print("\n-- 状态监听 %gs --" % args.seconds)
        t0 = time.time()
        while time.time() - t0 < args.seconds:
            s = ctrl.status()
            # 语音事件要单独占一行打印，否则会被上面的 \r 覆盖掉
            for kind, detail, _ts in ctrl.poll_voice_events():
                print("\n  [语音事件] %s：%s" % (kind, detail))
            if s["estop_locked"]:
                sys.stdout.write("\r  🔒 急停锁定中（%s）— release_estop 解除   "
                                 % s["estop_reason"])
            elif s["have_feedback"]:
                sys.stdout.write(
                    "\r  动作=%-4s 目标=%3d  Vx=%4d  Vz=%4d  电压=%5.2fV 急停=%d   "
                    % (s["action_label"], s["speed"], s["vx"], s["vz"],
                       s["voltage"], s["flag_stop"]))
            elif ctrl.connected:
                sys.stdout.write("\r  等待状态帧…（检查下位机是否上电、串口线是否接对）  ")
            else:
                sys.stdout.write("\r  （未连接串口，仅语音链路运行中）  ")
            sys.stdout.flush()
            time.sleep(0.1)
        print()
        ts = ctrl.tts_status()
        vs = ctrl.voice_status()
        print("\n语音播报 : %s" % (ts["msg"] or "未启用"))
        print("语音急停 : %s" % (vs["msg"] or "未启用"))
        if vs["hits"]:
            print("命中记录 : %s" % "、".join(h[0] for h in vs["hits"]))
        if ctrl.estop_locked:
            print("⚠️ 仍处于急停锁定态（%s）" % ctrl.estop_reason)
    except KeyboardInterrupt:
        print("\n中断")
    finally:
        ctrl.disconnect()
        ctrl.shutdown_voice()
    print("自测结束")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
