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
import tempfile
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
SPEED_MIN = 100                  # mm/s（协议安全下限，实测 100 能走）
SPEED_MAX = 500                  # mm/s（协议安全上限）
SPEED_STEP = 25
#: 默认速度（mm/s）。2026-10-28 落地实测后定为 **300**：
#:   · 协议里速度就是 mm/s，300 = 0.3 m/s（成人步行约 1.2 m/s 的 1/4）
#:   · 遥控器"手感丝滑"的前进实测均值 210 mm/s、峰值 253 mm/s，300 稍快一点，
#:     但演示时更利落；嫌快可切「慢」(200)
#:   · 旧值 30 是建立在"命令值≠mm/s"的错误标定上（实际只走 3 cm/s，落地像不动）
#: 详见 `cmd_to_mmps()` 上方那段说明。
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

# ══════════════════════════════════════════════════════════════════════
#  语音唤醒（wake word）—— 常量与环境变量（2026-10-12 新增）
# ══════════════════════════════════════════════════════════════════════
#
#  目标：说一句唤醒词 → 播**预录提示音** → 自动开录一句话 → ASR → 走**现有**
#        指令链路（`run_text_command`，规则优先 → LLM 兜底，安全闸门不变）。
#
#  🔴 三条硬件约束决定了整个设计（见 PROJECT_FACTS / `_yield_mic` 的注释）：
#     ① 采集与播放**双向互斥**（同一 xHCI）：播提示音前必须让 KWS 让位；
#     ② KWS 与录音**共用麦克风**（capture 只有 1 个 subdevice）：录音前 `_yield_mic()`；
#     ③ KWS 命中回调跑在监听线程里：**绝不能**在回调里录音/播报，只能投队列。
#
#  ★ 唤醒词必须并进**同一份 KWS 关键词表**（见 `_wake_keywords_file`）——
#    本板只有 1 个麦克风 subdevice，不可能给唤醒词另起一个常驻监听
#    （第二个 InputStream 必然 `-9999 Input/output error`）。

#: 默认唤醒词（逗号分隔的 `CAR_WAKE_WORDS` 可覆盖）。**不要选与一级急停词重合的词**。
DEFAULT_WAKE_WORDS = ("小车车",)

#: 唤醒词的拼音（sherpa KWS 关键词文件格式：声母/韵母分开、空格分隔、`@` 后接标签）。
#: ⚠️ 少一个音节都会**静默不命中**，所以不认识的词宁可报错也不要瞎拼。
WAKE_WORD_PINYIN = {
    "小车车": "x iǎo ch ē ch ē",
    "小车": "x iǎo ch ē",
    "你好小车": "n ǐ h ǎo x iǎo ch ē",
    "嘿小车": "h ēi x iǎo ch ē",
    "嗨小车": "h āi x iǎo ch ē",
}

#: 预录提示音目录（与本文件同级的 `wake_prompts/`）。
WAKE_PROMPT_DIR = "wake_prompts"
#: 主提示音（NPU TTS 合成）与兜底提示音（标准库 wave 生成的短嘟声）。
WAKE_PROMPT_FILE = "wake_zh.wav"
WAKE_PROMPT_BEEP = "wake_beep.wav"
#: 预录提示音缺失时回退的**极短** TTS 文本（本板 TTS RTF≈5，必须极短）。
WAKE_PROMPT_TEXT = "在听"

#: 唤醒会话的时间预算（秒）—— 都是**上限**，正常路径远小于它。
WAKE_MAX_SEC = 5.0          # 录音窗口上限（需求：最长 5s）
WAKE_SILENCE_SEC = 0.8      # 连续静音多久 → 提前结束（需求：0.8s）
WAKE_START_TIMEOUT = 4.0    # 一直没听到人说话 → 提前收工（省掉白等的 1s）
WAKE_MIN_SEC = 0.35         # 比这还短直接判「没听到」
WAKE_START_LEVEL = 0.02     # RMS ≥ 它 → 认为「开始说话了」
WAKE_SILENCE_LEVEL = 0.012  # RMS < 它 → 认为「这一刻是静音」
WAKE_POLL_SEC = 0.03        # 录音电平轮询间隔
WAKE_PLAY_MAX_SEC = 8.0     # 提示音播放上限（含 aplay 启动/排空）
WAKE_QUIET_WAIT_SEC = 1.0   # 提示音播完后等声卡真正安静的上限

#: `_restore_mic()` 的自动重试预算（需求：5 次 × 0.4s）。
#: 现状「失败一次就 failed」会让**语音急停永久失效**直到人工点按钮 —— 必须修。
WAKE_RESTORE_TRIES = 5
WAKE_RESTORE_DELAY = 0.15

#: 唤醒任务队列长度。满了就丢最旧的一条（唤醒是"要最新的那一句"）。
WAKE_QUEUE_MAX = 4


def _env_flag(name, default=True):
    """读一个开关型环境变量：未设/空 → default；0/false/no/off/… → False。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() not in (
        "0", "false", "no", "off", "none", "disable", "disabled")


def _wake_label_of(word):
    """从环境变量里的一个条目取出**标签**。

    允许两种写法：
        · `小车车`                      ← 普通写法，拼音查 `WAKE_WORD_PINYIN`
        · `x iǎo ch ē ch ē @小车车`     ← 直接给整行 sherpa 关键词
    """
    w = (word or "").strip()
    if "@" in w:
        return w.rsplit("@", 1)[1].strip()
    return w


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

# ══════════════════════════════════════════════════════════════════════
#  ★★ 速度单位真车标定（2026-10-17，卷尺实测，Atlas 板 + M500 麦轮）
# ══════════════════════════════════════════════════════════════════════
#
# 🔴 **重大发现：固件的速度命令值根本不是 mm/s。**
#    一直假设"速度 50 = 50mm/s"，于是 `duration = 距离/速度`。
#    实测（真车 + 卷尺，走廊里跑的）：
#        raw=25, 2.0s →  30 cm
#        raw=25, 3.0s →  95 cm
#        raw=30, 3.0s → 170 cm
#        raw≤20 无论多久都不动（电机死区）；raw=50 跑 4 秒就冲出去近 1 米
#
#    用 `d = V×(t − t_ramp/2)` 联立解得：
#        · **死区**           ≈ 18.6（命令值低于它电机不转）
#        · **稳态速度斜率**   ≈ 101.6 mm/s 每单位（即 V = 101.6×(cmd−18.6)）
#        · **加速时间**       ≈ 3.1 秒（很长！所以短脉冲几乎不动）
#    验证：raw=30 → V=1158mm/s → 3 秒应走 1158×(3−1.55)=1690mm，实测 1700mm ✅
#
# ⚠️ 这解释了为什么"前进一米"会冲出去：按旧代码 1000/300=3.33s，
#    而 3.33s 在 raw=300 下（若线性外推）是几十米。
#
# ⚠️ **高命令值（>40）未实测**，线性模型可能饱和。所以用 `SPEED_MMPS_MAX`
#    保守封顶，宁可算短一点（少走）也不要冲出去。
SPEED_DEADBAND = 0.0             # 实测无死区：命令30→回传30，命令150→回传149
SPEED_MMPS_PER_UNIT = 1.0        # ★ 协议里速度就是 **mm/s**，1 单位 = 1 mm/s
SPEED_RAMP_SEC = 0.8             # 遥控器实测：0→稳态约 0.8 秒（旧值 3.1 是错的）
SPEED_MMPS_MAX = 500.0           # 协议声明的安全范围上限 ±500 mm/s
#: 有实测支撑的命令值区间。超出这个区间的速度**只是外推**，报给用户时要提示。
SPEED_VALIDATED_MAX = 500


# ══════════════════════════════════════════════════════════════════════
#  🔴 2026-10-28：速度标定被**推翻重写**（真机落地实测）
#
#  旧模型：V = 101.6 × (cmd − 18.6) mm/s，声称 cmd=30 → 1158 mm/s。
#  真相：厂商协议 `car_serial.set_speed()` 的入参**就是 mm/s**
#        （源码注释：「设置目标速度 (mm/s)，安全范围 -500 ~ 500」），
#        串口回传的 `vx` 也是 mm/s。
#
#  铁证（用小车自带遥控器做已知动作，20 Hz 录回传帧积分）：
#      · 遥控前进"1 米"   → ∫vx·dt = 916 mm      （≈1 米 ✓）
#      · 遥控后退"0.5 米" → ∫vx·dt = −496 mm     （≈−0.5 米 ✓）
#      · 命令 150 → 回传 149；命令 300 → 回传 289~367（无死区、1:1）
#
#  旧模型把 mm/s 当成"自定义单位"又乘了 101.6，导致：
#      · 界面「慢/中/快」= 25/30/34 实际只发 **25~34 mm/s（3 cm/s）**
#        —— 落地时"车完全不动"，因为肉眼看不出来
#      · 所有 distance→duration 换算偏小约 40 倍（"前进一米"只走 7 cm）
#  所以这里的 1.0 / 0.0 不是"没标定"，是**标定结果就是恒等映射**。
# ══════════════════════════════════════════════════════════════════════


def cmd_to_mmps(cmd):
    """把固件速度命令值换算成 **mm/s**。

    标定结论：**恒等映射**（协议里命令值就是 mm/s），只做上限封顶。

    >>> cmd_to_mmps(300)   # → 300.0
    >>> cmd_to_mmps(150)   # → 150.0  （遥控器实测回传 149）
    >>> cmd_to_mmps(800)   # → 500.0  （封顶到协议安全上限）
    """
    try:
        c = float(cmd)
    except (TypeError, ValueError):
        return 0.0
    v = SPEED_MMPS_PER_UNIT * (c - SPEED_DEADBAND)
    if v <= 0:
        return 0.0
    return min(v, SPEED_MMPS_MAX)


# ── 实测距离补偿系数（2026-10-28 卷尺实测）────────────────────────────
#  同一条 3.73 s / 300 mm/s 的指令，实测距离却不同：
#      · 单条前进 1 米      → 100 cm  ✅
#      · 单条后退 1 米      →  90 cm  （方向不对称）
#      · 多步计划里的前进步 →  85 cm  （连续短动作，起步摩擦吃亏）
#  物理解释：每次从静止起步都要克服静摩擦，我们的斜坡补偿（ramp/2=0.4 s）
#  对"冷启动 + 连续短步"偏小。这里按实测量级做**保守补偿**，宁可多走一点点。
#  ⚠️ 这是经验值，n=2~3 次测量；若下次实测仍偏，按同比例再调。
BACKWARD_GAIN = 1.10        # 后退：90 → 期望 100 cm
PLAN_MOVE_GAIN = 1.15       # 计划内的直线步：85 → 期望 98 cm

def distance_efficiency(action, in_plan=False):
    """**模型距离 → 真实距离**的经验效率系数（<1 表示模型高估）。

    这是上面 BACKWARD_GAIN / PLAN_MOVE_GAIN 的逆运算，供自检脚本
    （`verify_presets.py`）把"模型算出来的毫米数"折成"真实会走多少"，
    免得补偿一加、自检就开始骗自己。
    """
    e = 1.0
    if action == "backward":
        e /= BACKWARD_GAIN
    if in_plan:
        e /= PLAN_MOVE_GAIN
    return e

def duration_for_distance(dist_mm, speed_cmd, lo=0.1, hi=12.0):
    """走 `dist_mm` 毫米需要多久（秒），用实测标定换算。

    🔴 旧实现是 `dist_mm / speed`（把命令值当 mm/s），实测错约 **20 倍** ——
       "前进一米"会跑十几米。这里改成先算真实速度、再补偿 3.1s 的加速时间：
           d = V×(t − t_ramp/2)  ⇒  t = d/V + t_ramp/2
       `t_ramp/2 = 1.55s` 这一项很重要：不加它，短距离会**明显走不够**。
    """
    v = cmd_to_mmps(speed_cmd)
    if v <= 0:
        return None
    t = float(dist_mm) / v + SPEED_RAMP_SEC / 2.0
    return round(min(max(t, lo), hi), 2)


def mmps_for_display(cmd):
    """给播报/界面用的"实际速度"（mm/s，取整）。"""
    return int(round(cmd_to_mmps(cmd)))


# ⚠️ 关于转向：下面的 DEG_PER_SEC_PER_UNIT = 0.375 是**上一代凭经验估的**，
#    用于把"左转 90 度"换算成"转多久"。但从 12V 控制板的源码看，
#    Z 轴控制量的物理意义是**角速度，单位 0.001 rad/s**（不是 mm/s）：
#      - 说明书第 18 页示例：Z 轴速度 0x0000 -> 角速度 0 "0.001rad/s"
#      - 固件 data_transition() 里 Z_speed 要除以轮距+轴距再乘 1000
#    也就是说 speed=200 对应的角速度就是 0.2 rad/s ≈ 11.46 度/秒，
#    而旧系数 0.375 度/秒对应 speed=200 只有 75 度/秒的 1/100 量级（见下）。
#    正确关系应为：角速度(度/秒) = Z控制量 × 0.001 × 180/π ≈ Z控制量 × 0.0573
#    因此这里改用**按物理量换算**，并保留旧值作为对照，实机务必标定确认。
#
# 🔴🔴 **2026-10-17 转向实车标定（原地旋转，量角器/目视）**
#
#    计划"先前进半米，再左转30度"在真车上**只前进、没转** —— 因为默认命令值 30
#    远低于转向死区。随后用命令值 100/200/300 各转 1.5 秒实测：
#         cmd=100 → <20°（≈不转）
#         cmd=200 → 20~50°（≈23°/s）
#         cmd=300 → >90°（≈60°/s）
#        方向正确（turn_left = 逆时针）
#    联立得：**角速度 ≈ 0.371 × (cmd − 138) °/s**，即
#        · 转向死区 ≈ **138**（远高于平移的 18.6 —— 所以 cmd 30 完全转不动）
#        · 斜率 ≈ 0.371 °/s 每单位
#    实测表明转向**几乎没有加速延迟**（1.5s 就基本是稳态），所以不加 ramp 补偿。
#
#    ⇒ `parse_text_command` 对"多少度"这类指令会**改用专用转向命令值 `TURN_CMD`**，
#       不再沿用平移速度（否则用户把速度调到 30 就永远转不了）。
DEG_PER_SEC_PER_UNIT = 0.375   # ⚠️ 旧经验值（保留兼容，已停用）
DEG_PER_SEC_PER_UNIT_RAD = 0.001 * 180.0 / 3.141592653589793  # ≈0.05730 理论值
#: 转向专用命令值。★ 2026-10-28 落地实测：**必须用大 Z**
#    Z=250 时车几乎不转（约 3°/s）；Z=1600 时实测约 **45°/s**。
#    按"Z=1600 → 45°/s"标定：90° 需 2.0 s、180° 需 4.0 s、一圈 8.0 s。
#    （旧注释写的 "250 → 41.6°/s" 是错的：那是把 45°/s 套在了 250 上，
#      实际 250 只有 ~3°/s，所以用户看到"转向幅度很小"。）
TURN_CMD = 1600
#: ★ 2026-10-28：转向的**命令值上限应与平移分开**。
#    证据：遥控器原地转 180° 时回传 vz 峰值 **1505**；而我们发 Z=500 时
#    vz 只有 212~304（约 5 倍差距）⇒ 转向通道的量程远大于平移的 ±500。
#    所以转向单独给 `TURN_MAX`，不再被 `SPEED_MAX=500` 卡住。
#    （先前扫描"Z=350/450/500 效果差不多"，其实是被 `_clamp_speed` 卡在 500 了。）
TURN_MAX = 1600
TURN_MIN = 100
#: 转向死区（命令值低于它原地转不动）
TURN_DEADBAND = 138.0
#: 转向斜率：角速度(°/s) = 斜率 × (cmd − 死区)
#:   由两点实测反解：Z=1600 命令90°(2.00s) 实测约 **80°** ⇒ ω≈40°/s；Z=250 几乎不转(≈3°/s)
#:   斜率 = 42.7/(1600−138) ≈ 0.0292（左右实测对称，不加单边补偿）
TURN_DEGPS_PER_UNIT = 0.0292
#: 角速度上限（°/s），防外推失控
TURN_DEGPS_MAX = 180.0


def turn_degps(cmd):
    """把固件速度命令值换算成**实测角速度**（度/秒）。

    >>> turn_degps(200)   # → 23.0   （实测 1.5s 转约 35°）
    >>> turn_degps(300)   # → 60.1   （实测 1.5s 转约 90°）
    >>> turn_degps(100)   # → 0.0    （低于转向死区，转不动）
    """
    try:
        c = float(cmd)
    except (TypeError, ValueError):
        return 0.0
    r = TURN_DEGPS_PER_UNIT * (c - TURN_DEADBAND)
    if r <= 0:
        return 0.0
    return min(r, TURN_DEGPS_MAX)


def duration_for_angle(deg, cmd=None, lo=0.1, hi=12.0):
    """转 `deg` 度需要多久（秒）。`cmd` 默认用转向专用命令值 `TURN_CMD`。"""
    r = turn_degps(TURN_CMD if cmd is None else cmd)
    if r <= 0:
        return None
    try:
        d = abs(float(deg))
    except (TypeError, ValueError):
        return None
    return round(min(max(d / r, lo), hi), 2)

# ── 多步指令规划（"先前进5米，再左转90°" → 分步执行）────────────────────
#
#  ⚠️ **单步指令必须逐字节等价**：`parse_plan()` 对任何单步输入一律返回 None，
#     上层继续走原来的 `parse_text_command()` + `_do_action()` 路径。
#     所以这里所有"放宽"都以"至少真有两个可执行步骤"为前提。
#
#  🔴 为什么默认**不逐步播报**（PLAN_SPEAK_STEPS=False）：
#     本板采集与播放**双向硬件互斥**（见 `_pause_kws_for_playback` 的注释）：
#     运动期间 KWS 占着麦克风，这时 `aplay` 报
#     `set_params:1416: Unable to install hw params` **必然失败**。
#     ⇒ 只在"全程开始前"说一句摘要（并**等它说完**再动车）、
#       "全部结束后"说一句完成语，中间一句都不说。
PLAN_MAX_STEPS = 3             # 一次最多规划几步（超过的截断，见 parse_plan）
PLAN_MAX_DISTANCE_MM = 1000    # 单步平移距离上限（mm），超过按上限截断
PLAN_MAX_ANGLE_DEG = 180       # 单步转角上限（度），超过按上限截断
#: ★ 2026-10-28 从 90 提到 180。依据：转向已实测标定 ω=0.371×(cmd−138)，
#:   TURN_CMD=250 → 41.6 °/s，**180° 只需 4.33 s**，远小于单条命令 8 s 上限，
#:   安全边界没变松；而 90° 上限会让「原地转一圈」根本做不出来
#:   （最多 3 步 × 90° = 270°，或单条 360° 被 8 s 砍成 332°）。
#:   提上去之后「两段 180°」就能凑出完整 360°。
#: 浮点容差：请求 90° 时 duration=round(90/41.552,2)=2.17 → 反算 90.17°，
#:   旧代码 90.17 > 90 成立会**误报限幅**，所以比较要带容差。
PLAN_ANGLE_EPS_DEG = 0.6
PLAN_STEP_SETTLE_SEC = 0.25    # 每步之间的稳定间隔（秒）
PLAN_SPEAK_STEPS = False       # 是否**逐步**播报（默认 False，理由见上）
PLAN_SPEAK_WAIT_SEC = 8.0      # 等计划摘要播完的上限（秒；正常路径远小于它）
PLAN_ABORT_POLL_SEC = 0.1      # 计划等待期间轮询中止信号的间隔（秒）
#: 计划里允许的**运动类**动作（速度类/停车绝不进计划）。
PLAN_MOTION_ACTIONS = ("forward", "backward", "turn_left", "turn_right")
#: 多步连接词 —— 只有出现它们才按多步处理（顺序：多字在前，避免被单字抢走）。
PLAN_CONNECTORS = ("然后", "接着", "之后", "随后", "再", "；", ";")
#: 拆片段时要去掉的两端字符（空白 + 中英文标点）。
_PLAN_FRAG_STRIP = " \t\r\n，,。.、!！?？~～"
#: 计划播报文案（**全部 ≤12 字** —— TTS 单段上限，见 llm_engine.SAY_MAX_CHARS）。
SPEAK_PLAN_START = "分%s步开始"     # 例：分三步开始（5 字）
SPEAK_PLAN_DONE = "%s步都完成了"    # 例：三步都完成了（6 字）
SPEAK_PLAN_ABORT = "已中止"         # 3 字
SPEAK_PLAN_STEP = "第%s步开始"      # 仅在 PLAN_SPEAK_STEPS=True 时用（5 字）


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

        # ★ 先把速度钳好，后面换算要用它的"真实 mm/s"。
        #   ⚠️ 转向要按**转向量程**钳位（TURN_MAX=1600），不能套平移的 500 ——
        #      否则"以1200的速度左转"会被悄悄砍成 500，转起来还是那么小。
        speed = (_clamp_turn(speed) if action in ("turn_left", "turn_right")
                 else _clamp_speed(speed))

        # 4c) 距离：一米 / 1.5m / 200毫米
        #     🔴 用**实测标定**换算，不是 `距离/命令值`（那样错约 20 倍）
        if duration is None:
            dist_mm = None
            m = re.search(_NUM_GROUP + r"\s*(?:毫米|mm)", t)
            if m:
                v = _to_number(m.group(1))
                if v is not None:
                    dist_mm = v
            else:
                m = re.search(_NUM_GROUP + r"\s*(?:米|m|M)(?![毫mM米])", t)
                if m:
                    v = _to_number(m.group(1))
                    if v is not None:
                        dist_mm = v * MM_PER_METRE
            if dist_mm is not None:
                duration = duration_for_distance(dist_mm, speed)
                # 后退实测偏短 10%（卷尺实测 90 vs 100 cm）→ 补偿
                if duration and action == "backward":
                    duration *= BACKWARD_GAIN

        # 4d) 角度：90度 / 90°
        #     🔴 2026-10-17 实测：转向死区高达 **138**，默认命令值 30 根本转不动；
        #        所以这里**改用专用转向命令值 TURN_CMD**，并按实测角速度换算时长。
        if duration is None:
            m = re.search(_NUM_GROUP + r"\s*(?:度|°|deg)", t)
            if m:
                v = _to_number(m.group(1))
                if v is not None:
                    speed = _clamp_turn(TURN_CMD)
                    duration = duration_for_angle(v, speed)

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


def _clamp_turn(v):
    """转向命令值的钳位。**量程与平移不同**（见 TURN_MAX 的说明）。"""
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = TURN_CMD
    return max(TURN_MIN, min(TURN_MAX, v))



#: 多步单步时长的下限/上限（秒）
PLAN_DUR_MIN = 0.1
PLAN_DUR_MAX = 30.0


def _plan_dur_floor(dur, lo=PLAN_DUR_MIN, hi=PLAN_DUR_MAX):
    """多步时长：先钳位，再**向下取整**到 0.01 秒。

    🔴 为什么不能用 `round()`（2026-10-17 独立验证挖出的 B2）：
       `duration` 会被反算回"距离/角度"去核对上限，而 `round(x, 2)` 会
       **进位** ⇒ 实际值略微超过限幅上限。
       实测最坏情况（扫描 speed=50..500）：
         · `round(1000/475, 2) = 2.11s` → 2.11×475 = **1002.25 mm**（上限 1000）
         · `round(90/(466×0.375), 2) = 0.52s` → **90.87°**（上限 90）
       多步限幅是**安全上限**（"一句话别把车开飞"），宁可少走一点也不能超。
      ⇒ 改用向下取整，保证反算出来的距离/角度**恒 ≤ 上限**。
    """
    try:
        d = float(dur)
    except (TypeError, ValueError):
        d = lo
    if d < lo:
        d = lo
    elif d > hi:
        d = hi
    return int(d * 100) / 100.0


# ══════════════════════════════════════════════════════════════════════
#  多步指令解析（规则层）
# ══════════════════════════════════════════════════════════════════════
def _plan_log(log, msg):
    """多步解析的日志：有 log 回调就走回调，否则打到 stdout（绝不静默）。

    模块级函数拿不到 `CarController.log`，所以用"回调注入"的方式；
    `run_text_command()` 里传 `self.log`，解析过程就能进界面日志。
    """
    try:
        if log is not None:
            log(msg)
        else:
            print("[规划] %s" % msg, flush=True)
    except Exception:
        pass


def _plan_num_cn(n):
    """计划步数的中文读法（2 → "两"，比"二步"自然；其余走 num_to_cn）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    return "两" if n == 2 else num_to_cn(n)


def _plan_trim(frag):
    """清理一个片段：去两端空白/标点，并去掉"先…再…"的"先"字前缀。"""
    s = (frag or "").strip(_PLAN_FRAG_STRIP)
    if s.startswith("先"):
        s = s[1:].strip(_PLAN_FRAG_STRIP)
    return s


def parse_plan(text, default_speed=DEFAULT_SPEED, log=None):
    """
    规则层**多步**解析：把「先前进一米，再左转90度」拆成 2 步。

    返回 `list[dict]`（每项 {"action","speed","duration"}，可直接喂给
    `_do_action`），或者 **None** —— None 的语义是"这不是一个可执行的多步
    指令，请走原来的单步路径"。

    ⚠️ **返回 None 的所有条件**（这些条件共同保证了**单步行为零变化**）：
      · 没有多步连接词（再/然后/接着/之后/随后/;/；）→ 压根不是多步；
      · 清理后不足 2 段；
      · 任一段解析不出动作，或解析出**非运动类**（速度类/set_speed）；
      · 任一段**没有明确时长/距离/角度**（即无界运动）→ 退回单步，
        因为计划里不允许"一直走"这种步骤；
      · 任一段解析出 **stop** → 退回单步（让上层走原来的急停/停车路径，
        **绝不在 stop 之后再规划任何运动**）。
    步数超过 `PLAN_MAX_STEPS` 时：**前 3 步照常执行，多出来的片段忽略**并 log；
    ⚠️ 但被忽略的片段里只要有**停车词**，整个计划仍退回单步（停车优先，
    绝不能出现"说了停车、车却还在按计划走"）。
    """
    if not text or not str(text).strip():
        return None
    t = str(text).strip()

    # ① 没有多步连接词 → 直接返回 None（单步路径**零开销、零日志**）
    if not any(c in t for c in PLAN_CONNECTORS):
        return None

    # ② 按连接词切片段（长的在前，保证"然后"不会被"再"之类的单字抢走）
    pat = "|".join(re.escape(c) for c in PLAN_CONNECTORS)
    frags = [_plan_trim(f) for f in re.split(pat, t)]
    frags = [f for f in frags if f]
    if len(frags) < 2:
        return None

    # ③ 逐段解析 + 逐段限幅
    steps = []
    ignored = []
    for frag in frags:
        # ★ 已经够 PLAN_MAX_STEPS 步了：后面的片段**不再规划**，但只做一件事 ——
        #   万一里面有停车词，立刻整体退回单步（安全优先，见 docstring）。
        if len(steps) >= PLAN_MAX_STEPS:
            res, _m = parse_text_command(frag, default_speed)
            if res is not None and res.get("action") == "stop":
                _plan_log(log, "多步解析：被忽略的片段「%s」是停车 → 退回单步"
                          "急停路径" % frag)
                return None
            ignored.append(frag)
            continue
        res, _msg = parse_text_command(frag, default_speed)
        if res is None:
            _plan_log(log, "多步解析：片段「%s」没听懂，退回单步" % frag)
            return None
        act = res.get("action")
        # ★ 停车是安全动作：**必须**走原来的单步急停/停车路径，绝不排进计划
        if act == "stop":
            _plan_log(log, "多步解析：片段「%s」是停车 → 退回单步急停路径" % frag)
            return None
        if act not in PLAN_MOTION_ACTIONS:
            _plan_log(log, "多步解析：片段「%s」是非运动动作（%s），退回单步"
                      % (frag, act))
            return None
        dur = res.get("duration")
        if dur is None:
            _plan_log(log, "多步解析：片段「%s」没有明确时长/距离/角度，不规划"
                      % frag)
            return None
        speed = (_clamp_turn(res.get("speed") or default_speed)
                 if act in ("turn_left", "turn_right")
                 else _clamp_speed(res.get("speed") or default_speed))
        dur = float(dur)
        # 限幅：**必须用实测标定反算**（命令值不是 mm/s、转向另有死区 138）
        if act in ("forward", "backward"):
            v = cmd_to_mmps(speed)
            dist_mm = v * max(0.0, dur - SPEED_RAMP_SEC / 2.0) if v > 0 else 0.0
            if dist_mm > PLAN_MAX_DISTANCE_MM:
                dur = duration_for_distance(PLAN_MAX_DISTANCE_MM, speed) or dur
                _plan_log(log, "多步限幅：「%s」距离 %.0fmm 超过上限 → 截到 %dmm"
                          "（%.2fs）" % (frag, dist_mm, PLAN_MAX_DISTANCE_MM, dur))
        else:
            r = turn_degps(speed)
            ang = dur * r
            if ang > PLAN_MAX_ANGLE_DEG + PLAN_ANGLE_EPS_DEG:
                dur = duration_for_angle(PLAN_MAX_ANGLE_DEG, speed) or dur
                _plan_log(log, "多步限幅：「%s」角度 %.0f° 超过上限 → 截到 %d°"
                          "（%.2fs）" % (frag, ang, PLAN_MAX_ANGLE_DEG, dur))
        # ★ 计划内连续短动作实测偏短 15%（冷启动摩擦）→ 补偿
        if act in ("forward", "backward"):
            dur *= PLAN_MOVE_GAIN
        dur = _plan_dur_floor(dur)
        steps.append({"action": act, "speed": speed, "duration": dur})

    if len(steps) < 2:
        return None
    if ignored:
        _plan_log(log, "多步解析：计划超过上限 %d 步 → 只执行前 %d 步，忽略：%s"
                  % (PLAN_MAX_STEPS, PLAN_MAX_STEPS, "、".join(ignored)))
    return steps


def _plan_step_desc(step):
    """把计划里的一步写成日志用的一行描述（例：前进 300mm/s 3.33s）。"""
    try:
        act = step.get("action")
        label = ACTION_LABEL.get(act, act)
        dur = step.get("duration")
        return "%s %smm/s %s" % (label, step.get("speed"),
                                 ("%.2fs" % dur) if dur else "持续")
    except Exception:
        return str(step)


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

        # ── 语音唤醒（wake word）────────────────────────────────────
        # 唤醒词命中 → 投队列（**绝不在 KWS 回调里干活**）→ `_wake_worker`
        # 顺序做「播预录提示音 → 录音 → ASR → run_text_command」。
        # 详见 `_on_kws_hit` / `_do_wake_session` 的注释。
        self._wake_q = Queue(maxsize=WAKE_QUEUE_MAX)   # 唤醒任务 [(标签, 时刻)]
        self._wake_stop = threading.Event()            # 工作线程退出信号
        self._wake_abort = None                        # 当前会话的取消 Event（或 None）
        self._wake_lock = threading.Lock()             # 保护工作线程的创建
        self._wake_thread = None
        #: 唤醒功能的**运行期开关**（`_wake_enabled()` 读它）。
        #: ⚠️ 变量名**不能**叫 `_wake_enabled` —— 那会把同名方法 `_wake_enabled()`
        #:    覆盖掉，调用时直接 TypeError（2026-10-12 板上实测踩到）。
        self._wake_on = _env_flag("CAR_WAKE_ENABLE", True)
        self._wake_word_list = None      # set_wake_words() 的覆盖值（None = 用环境变量）
        self._wake_path = os.path.join(_HERE, WAKE_PROMPT_DIR)   # 提示音目录
        self._wake_kw_file = None        # 上次成功写出的"合并关键词表"路径
        self._wake_prompt = ""           # 实际选中的提示音文件（"" = 没找到）
        self._wake_state = "off"         # off/idle/prompt/recording/recognizing/executing/error/locked
        self._wake_msg = "未开启"
        self._wake_text = ""             # 最近一次唤醒识别出的文本
        self._wake_ts = 0.0              # 上面那次文本的时刻
        self._wake_count = 0             # 成功识别的唤醒次数
        self._wake_busy = False          # 是否有唤醒会话正在跑（界面显示"识别中…"）
        self._wake_last_label = ""       # 最近命中的唤醒词
        #: 实测用（也供界面显示）：唤醒→开录音耗时 / 录音时长 / ASR 耗时
        self._wake_last_rec_start = 0.0
        self._wake_last_open_cost = 0.0
        self._wake_last_record_sec = 0.0
        self._wake_last_asr_sec = 0.0
        #: ★ ASR 引擎由界面注入（见 `set_asr_engine`）。唤醒会话要用它做识别；
        #:   没注入时 `_resolve_wake_asr()` 会**按需**懒加载一次（有日志，不静默）。
        self._asr = None
        self._asr_lock = threading.Lock()
        self._asr_probe_done = False
        self._asr_probe_note = ""

        #: ★ 麦克风是不是正被"录音"拿着（含唤醒会话）。
        #:   让位前先看它，避免两路录音互相抢麦克风。见 `_yield_mic`。
        self._mic_busy = False
        #: ★ 转向专用速度（`TURN_CMD`）。**与 `_speed` 分开**：
        #:   转向命令值要大于直行死区才能转得动，但它不是"用户设定的车速"，
        #:   写回 `_speed` 会让下一次前进全速冲（2026-10-28 修）。
        self._turn_speed = TURN_CMD
        #: ★ 麦克风的**长期占用者**（`claim_mic()` / `release_mic()` 管）。
        #:   与 `_mic_busy` 的区别：`_mic_busy` 是"当下这一刻在录音"，
        #:   而这里是"这一整段时间归谁"——自动聆听要反复开关设备，
        #:   中间任何一次播报结束后的 `_restore_mic()` 都不能把 KWS 抢回去。
        #:   取值：None / "自动聆听" / "按住说话" / "唤醒会话" …
        self._mic_claim = None

        # ── 急停锁定态 ──────────────────────────────────────────────
        # 语音（或手动）急停后**不自动恢复**，必须人工 release_estop()。
        # 理由：误触发之后车不会自己又跑起来，这是安全底线。
        self._estop_locked = False
        self._estop_reason = ""

        # ── 多步计划（"先前进5米，再左转90°"）────────────────────────
        #: 计划中止信号：`trigger_emergency()` 会 **set** 它 ⇒ 正在跑的
        #: `_run_plan()` 立刻停下（不能等它下一步才发现）；
        #: `release_estop()` 里 **clear**。计划中途收到"停车"也会 set。
        #: ⚠️ 中止后**绝不自动续跑**，必须重新下指令（刻意设计，别加"恢复"）。
        self._plan_abort = threading.Event()
        #: 是否有计划正在执行（供"中途停车要中止计划"判断，单步不受影响）。
        self._plan_running = False
        #: ★ 多步进度 k/n（2026-10-28 加，给网页对话卡片用）。
        #: `_plan_step` 从 1 开始；`status()` 暴露成 plan_step/plan_total/plan_label。
        #: ⚠️ 只在"计划执行中"有意义，计划结束会归零。
        self._plan_step = 0
        self._plan_total = 0
        self._plan_label = ""
        #: ★ 保护"检查 `_plan_running` 并占位"的原子性（2026-10-17）。
        #: 计划修到**后台线程**执行后（B1），两条指令可能同时到达，
        #: 只用布尔标志会出现"两个线程都通过检查"的竞态。
        self._plan_lock = threading.Lock()
        #: 上一次中止是不是**急停**造成的（急停已经会播「已急停」，
        #: 计划就不要再补一句「已中止」把那条顶掉 —— 见 `_run_plan` 收尾）。
        #: ⚠️ 必须在 set `_plan_abort` **之前**置位，否则计划线程可能先醒来看不到。
        self._plan_abort_by_estop = False

        # ── LLM（指令纠错 + 对话）—— 本地或云端 ─────────────────────
        # 只在**规则快路径没命中**时才被调用（见 run_text_command）。
        self._llm = None
        self._llm_state = "off"         # off / ready / failed
        self._llm_msg = ""
        self._llm_enabled = True        # 总开关（界面可关）
        self._llm_mode = "auto"         # auto / api / server / cli
        self._llm_replies = deque(maxlen=30)   # 近期对话，供界面展示

        # 唤醒工作线程：一旦构造就常驻（daemon，阻塞在队列上，不占 CPU）。
        # ★ 为什么不懒启动：`_on_kws_hit` 里的分支要求"投完立刻返回"，
        #   在那里现起线程（拿锁 + start）虽然也就几十微秒，但没必要。
        self._ensure_wake_worker()

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

        # ★ 转向 vs 直行：**速度必须分开记**（2026-10-28 真机踩到）
        #    转向要用专用命令值 `TURN_CMD`（死区 138，见 turn_degps），
        #    但绝不能把它写回 `_speed` —— 否则一次「左转30度」之后，
        #    下一句「前进一秒」会按 250（≈1.5 m/s）冲出去（落地很危险）。
        #    真机现象：status.speed 从 30 变成 250，前进一秒变成全速冲。
        if action in ("turn_left", "turn_right"):
            sp = _clamp_turn(TURN_CMD if speed is None else speed)
            self._turn_speed = sp
        else:
            sp = _clamp_speed(speed if speed is not None else self._speed)
            self._speed = sp
        kx, ky, kz = ACTION_XYZ[action]
        try:
            self._write_speed(kx * sp, ky * sp, kz * sp)
        except Exception as e:
            self.log("下发指令异常：%s" % e)
            return False

        self._action = action
        # 日志报**真实物理量**：平移报 mm/s、转向报 °/s（命令值只在括号里）。
        # 🔴 别再写成 "mm/s" 了 —— 命令值不是 mm/s（实测差约 20 倍），
        #    日志误导会让人拿错数据去调参。
        if action in ("forward", "backward"):
            phys = "约 %d mm/s" % cmd_to_mmps(sp)
        elif action in ("turn_left", "turn_right"):
            phys = "约 %.1f °/s" % turn_degps(sp)
        else:
            phys = ""
        tag = "%s  命令值%d%s" % (ACTION_LABEL[action], sp,
                                   ("（%s）" % phys) if phys else "")
        if duration:
            self.log("%s  %.2f s" % (tag, duration))
            self._schedule_stop(float(duration))
        else:
            self.log("%s  持续" % tag)
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

        ★ 唯一的例外（2026-10-17，多步计划落地时加）：顺手 `_plan_abort.set()`
          叫停**正在执行的多步计划**。否则"急停发了停车帧、计划却在 0.25s 后
          又下发下一步"—— 急停等于白按。对单步指令**零影响**（没有计划在跑时
          只是置了一个没人读的 Event，下一次计划启动时会先 clear）。

        ⚠️ `_plan_abort.set()` 要放在"未连接早退"**之前**：离线演示模式下
          计划也可能在跑（车没连、但会走演示逻辑），那时急停同样要中止它。
        """
        # 先置"这次中止是急停造成的"标记，**再** set 事件 —— 顺序不能反
        # （计划线程可能一醒来就检查标记，见 `_plan_abort_by_estop`）。
        self._plan_abort_by_estop = True
        self._plan_abort.set()
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

    def _llm_reply_to_plan(self, reply):
        """
        把 LLM 答复里的**多步**指令（`reply.steps`）转成计划。

        跨队友契约（llm_engine 侧负责产出，这里只消费）：
            LlmReply.steps = [{"cmd": "forward"|"backward"|"turn_left"|"turn_right",
                               "speed": int|None,
                               "distance_mm": int|None,
                               "angle_deg": int|None}, ...] 或 None
        ⇒ 本方法把它换算成 `_run_plan()` 能直接吃的
          `{"action","speed","duration"}`，并做**与规则层完全一致**的限幅。

        返回 list[dict] 或 None（None = 没有可用多步 → 退回原来的单步/闲聊逻辑）。

        ⚠️ 安全（与 `_llm_reply_to_result` 同一套门槛，一样都不能少）：
          · 报错 / 置信度低于 `LLM_MIN_CONFIDENCE` / 疑似停车语义 → None；
          · 动作只允许**运动类**（速度类、stop 一律不收）；
          · 缺距离/角度的步骤 → 整个计划作废（绝不做"无界运动"）；
          · 步数/距离/角度都按 PLAN_MAX_* 截断。

        ⚠️ 角度换算用 `DEG_PER_SEC_PER_UNIT` = 0.375 度/秒/单位，**该系数尚未
          实机标定**（理论值见 `DEG_PER_SEC_PER_UNIT_RAD` ≈ 0.0573，两者差 6.5 倍）。
          多步里转向误差会累积，首次实机务必用卷尺/量角器复核。
        """
        if reply is None:
            return None
        if getattr(reply, "error", ""):
            return None
        raw_steps = getattr(reply, "steps", None)
        if not raw_steps:
            return None
        try:
            conf = float(getattr(reply, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < LLM_MIN_CONFIDENCE:
            self.log("LLM 多步计划置信度 %.2f < %.2f，不下发" % (conf, LLM_MIN_CONFIDENCE))
            return None
        say = getattr(reply, "say", "") or ""
        try:
            if _llm_engine.looks_like_stop(say):
                self.log("LLM 多步计划疑似停车语义（say=%r），改走单步/闲聊"
                         % say[:20])
                return None
        except Exception:
            pass

        out = []
        for i, st in enumerate(raw_steps):
            n = i + 1
            if not isinstance(st, dict):
                self.log("LLM 多步计划第 %d 步格式不对（%r），整个计划作废" % (n, st))
                return None
            cmd = st.get("cmd")
            if cmd not in PLAN_MOTION_ACTIONS:
                self.log("LLM 多步计划第 %d 步动作非法（%r），整个计划作废"
                         % (n, cmd))
                return None
            try:
                speed = _clamp_speed(st.get("speed") or self._speed)
            except (TypeError, ValueError):
                speed = _clamp_speed(self._speed)
            dur = None
            if cmd in ("forward", "backward"):
                dist = st.get("distance_mm")
                try:
                    dist = float(dist) if dist is not None else None
                except (TypeError, ValueError):
                    dist = None
                if dist is None or dist <= 0:
                    self.log("LLM 多步计划第 %d 步缺 distance_mm，整个计划作废" % n)
                    return None
                if dist > PLAN_MAX_DISTANCE_MM:
                    self.log("LLM 多步限幅：第 %d 步 %.0fmm 超过上限 → 截到 %dmm"
                             % (n, dist, PLAN_MAX_DISTANCE_MM))
                    dist = float(PLAN_MAX_DISTANCE_MM)
                # ★ 用实测标定换算（命令值不是 mm/s，见 cmd_to_mmps）
                dur = duration_for_distance(dist, speed)
                if dur is None:
                    self.log("LLM 多步计划第 %d 步距离换算失败，整个计划作废" % n)
                    return None
            else:
                ang = st.get("angle_deg")
                try:
                    ang = abs(float(ang)) if ang is not None else None
                except (TypeError, ValueError):
                    ang = None
                if ang is None or ang <= 0:
                    self.log("LLM 多步计划第 %d 步缺 angle_deg，整个计划作废" % n)
                    return None
                if ang > PLAN_MAX_ANGLE_DEG + PLAN_ANGLE_EPS_DEG:
                    self.log("LLM 多步限幅：第 %d 步 %.0f° 超过上限 → 截到 %d°"
                             % (n, ang, PLAN_MAX_ANGLE_DEG))
                    ang = float(PLAN_MAX_ANGLE_DEG)
                # ★ 转向用专用命令值 + 实测角速度模型（死区 138，见 turn_degps）
                speed = _clamp_turn(TURN_CMD)
                dur = duration_for_angle(ang, speed)
                if dur is None:
                    self.log("LLM 多步计划第 %d 步角度换算失败，整个计划作废" % n)
                    return None
                self.log("LLM 多步：第 %d 步 %.0f° → 命令值%d 转 %.2fs"
                         "（实测角速度 %.1f°/s）"
                         % (n, ang, speed, dur, turn_degps(speed)))
            dur = _plan_dur_floor(dur)
            out.append({"action": cmd, "speed": speed, "duration": dur})

        if len(out) < 2:
            return None
        if len(out) > PLAN_MAX_STEPS:
            self.log("LLM 多步计划 %d 步超过上限 %d 步 → 只执行前 %d 步"
                     % (len(out), PLAN_MAX_STEPS, PLAN_MAX_STEPS))
            out = out[:PLAN_MAX_STEPS]
        return out

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

        # ── 多步计划（"先前进一米，再左转90度"）─────────────────────────
        #   ★ 只在**真能拆出 ≥2 步运动指令**时才返回非 None；单步输入一律 None，
        #     于是下面原有的单步执行路径**逐字节不变**（这是本轮的硬要求）。
        #   ★ 计划优先于规则层的"第一个命中"：复合句里单步解析只会拿到其中
        #     一个动作（例「先前进一米，再左转90度」原来会被解析成"左转"）。
        plan = parse_plan(text, self._speed, log=self.log)
        if plan:
            return self._run_plan(plan)

        # ── 规则没命中：交给 LLM 纠错 / 闲聊 ────────────────────────
        if result is None:
            reply = llm_hint if llm_hint is not None else self._ask_llm(text)
            if reply is not None and not reply.error:
                # ① LLM 一次给出**多步**时，优先按计划分步执行
                llm_plan = self._llm_reply_to_plan(reply)
                if llm_plan:
                    self.log("LLM 多步计划：「%s」→ %d 步" % (text, len(llm_plan)))
                    return self._run_plan(llm_plan)
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

        # ★ 多步计划执行期间收到"停车"→ 立刻叫停计划（绝不让它 0.25s 后又下发
        #   下一步，否则用户说的"停车"等于白说）。计划自己的每一步都是运动类，
        #   不可能走到这里，所以对**单步**行为零影响（`_plan_running` 恒为 False）。
        if act == "stop" and getattr(self, "_plan_running", False):
            self._plan_abort.set()

        # ── 速度类：本来就是纯本地状态，不需要小车，也顺带补上语音应答 ──
        #   ⚠️ 播报前必须过 `speak_text()`：NPU TTS 读不出阿拉伯数字，
        #      "加速到313" 只会听到"加速到"就戛然而止（见 `_CN_DIGITS` 的说明）。
        #   🔴 2026-10-17：播报要报**真实 mm/s**（`mmps_for_display`），不能报固件
        #      命令值 —— 实测命令值 ≠ mm/s（raw=30 实际约 1158mm/s），
        #      否则用户听到"速度30毫米每秒"而车其实在跑 1.1m/s，会严重误判。
        if act == "set_speed":
            v = self.set_speed_value(result["value"])
            if self._announce_actions:
                self.speak(speak_text("速度%d%s" % (mmps_for_display(v), SPEED_UNIT_CN)),
                           drop_pending=True)
            self.log("%s速度设为 命令值%d（实测约 %d mm/s）"
                     % (prefix, v, mmps_for_display(v)))
            return True, "速度 %d" % v
        if act == "speed_up":
            v = self.speed_up()
            if self._announce_actions:
                self.speak(speak_text("加速到%d%s" % (mmps_for_display(v), SPEED_UNIT_CN)),
                           drop_pending=True)
            self.log("%s加速 → 命令值%d（实测约 %d mm/s）"
                     % (prefix, v, mmps_for_display(v)))
            return True, "速度 %d" % v
        if act == "speed_down":
            v = self.speed_down()
            if self._announce_actions:
                self.speak(speak_text("减速到%d%s" % (mmps_for_display(v), SPEED_UNIT_CN)),
                           drop_pending=True)
            self.log("%s减速 → 命令值%d（实测约 %d mm/s）"
                     % (prefix, v, mmps_for_display(v)))
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
    #  多步计划执行器（"先前进5米，再左转90°" → 分步执行）
    # ══════════════════════════════════════════════════════════════════
    #
    #  🔴 为什么必须"自己等每一步跑完"（最容易写错的地方）：
    #     `_do_action()` → `move()` 只是**下发速度帧 + 起一个 threading.Timer
    #     定时停车**，它**立即返回**。如果写成
    #         for step in steps: self._do_action(step)      ← 错的
    #     第二步就会在第一步还没跑完时下发，两帧互相覆盖，整条计划退化成
    #     "只有最后一步"（上一轮别人踩过这个坑）。
    #     ⇒ 每步之后必须等 `duration + PLAN_STEP_SETTLE_SEC`，
    #       而且要用 `_plan_abort.wait()` 轮询（可被急停立刻打断），不能裸 sleep。
    #
    #  ★ 单步零影响：本文件之外没人调这个执行器；`parse_plan()`/`_llm_reply_to_plan()`
    #    都保证进来时 ≥2 步，单步输入永远走原来的 `_do_action()` 路径。
    def _plan_not_alive_reason(self):
        """计划还能继续吗？能 → 返回空串；不能 → 返回**人话原因**。"""
        if self._plan_abort.is_set():
            return "已收到中止信号（急停/停车/人工中止）"
        if self._estop_locked:
            return "急停锁定中"
        # 与 `_do_action` 的落地判断保持一致：已连接，或"未连接但开了离线演示"。
        if not (self._connected and self._car is not None) and not self._demo:
            return "与小车连接已断开"
        return ""

    def _plan_wait(self, seconds):
        """可中止的等待。返回 True=继续，False=该中止了。

        ⚠️ 用 `self._plan_abort.wait()` 而不是裸 `time.sleep()`：
           `trigger_emergency()` 会 set 它 → 这里**立刻**返回，
           不会出现"急停下了、计划还在傻等 3 秒"。
        """
        deadline = time.time() + max(0.0, float(seconds or 0.0))
        while True:
            if self._plan_not_alive_reason():
                return False
            remain = deadline - time.time()
            if remain <= 0:
                return True
            # 分片等：即使中止信号不是 Event（比如别人直接置 _estop_locked），
            # 也能在 PLAN_ABORT_POLL_SEC 内发现。
            self._plan_abort.wait(min(remain, PLAN_ABORT_POLL_SEC))

    def _wait_plan_speech(self, max_wait=PLAN_SPEAK_WAIT_SEC):
        """等一句计划播报**真的说完**再动车。返回是否已安静。

        ★ 为什么必须等：本板采集/播放**双向硬件互斥**，运动期间 `aplay` 必失败
          （`set_params:1416`）。所以"分三步开始"这句话必须完整播完才允许第一步
          下发速度帧 —— 用 `speak_idle()`（队列空 + 声卡真的不响）判断，
          而不是 `_wait_playback_quiet()`（那只等 1 秒、不等合成）。

        超时也只 log 不抛：**播报失败绝不能影响车的动作**（安全底线，
        与 `speak()` 的"允许失败"约定一致）。

        ⚠️ 超时**必须抢占播报**（2026-10-17 真机实测踩到）：
          本板 CPU 兜底 TTS（Kokoro）RTF≈5，一句 5 字摘要要 ~8s 才播完；
          若只是"超时就往下走"，那句话会**拖到第 1 步运动期间还在响** ——
          正是本模块要避免的硬件互斥场景。所以超时就 `yield_speaker()`
          （现成封装：丢掉排队 + 掐掉 aplay + 等声卡真安静）再动车。
        """
        t0 = time.time()
        while time.time() - t0 < max_wait:
            if self._plan_not_alive_reason():
                return False
            if self.speak_idle():
                return True
            self._plan_abort.wait(0.05)
        # 超时 → 掐掉这句话，保证"运动中不出声"优先于"把摘要说完"
        try:
            quiet, why = self.yield_speaker(max_wait=1.0)
            self.log("⚠ 计划摘要播报超时（>%.0fs）→ 已抢占播报（%s），继续执行"
                     % (max_wait, why))
        except Exception as e:
            self.log("⚠ 计划摘要播报超时，抢占播报异常（已忽略）：%r" % (e,))
        return True

    def _run_plan(self, steps):
        """
        执行多步计划，返回 (是否全部完成, 提示信息)。

        设计要点（每条都有理由，别随手改）：
          · 每一步都走 **`_do_action()` 统一入口** → 急停锁定/连接/离线演示
            这套安全闸门只有一份，计划不会绕开它；
          · 每步之间等 `duration + PLAN_STEP_SETTLE_SEC`（见上面"为什么"）；
          · 每步**开始前**检查 `_plan_abort` / `_estop_locked` / 连接 →
            任一不满足**立刻中止并清空剩余步骤**；
          · 计划期间**不发逐步动作播报**（PLAN_SPEAK_STEPS=False，硬件互斥）；
            只在"开始前"说一句摘要（等它播完）、"结束后"说一句完成语；
          · 中止后**绝不自动续跑**（必须重新下指令，刻意设计）。

        `steps`：`[{"action","speed","duration"}, ...]`，来自 `parse_plan()`
        或 `_llm_reply_to_plan()`，长度保证 ≥ 2（这里仍做防御性判断）。
        """
        steps = list(steps or [])
        if not steps:
            return False, "计划为空"
        if len(steps) < 2:
            # 防御：单步计划不该走到这里（两个入口都保证 ≥2 步）。
            # 万一真发生，退回**原单步路径**，行为与改动前一致。
            st = steps[0]
            return self._do_action(st, ACTION_LABEL.get(st.get("action"), ""))

        n = len(steps)

        # ① 开局安全检查（与 _do_action 同源：锁定 → 重入 → 中止信号 → 连接/演示）
        if self._estop_locked:
            self.log("多步计划被拒绝：急停锁定中（先 release_estop）")
            return False, "急停锁定中"

        # ★★ 重入保护（2026-10-17 补：B1 修完之后才真正需要）
        #
        # 🔴 为什么现在必须有：
        #   修 B1 之前，计划跑在**界面线程**里同步执行 ⇒ 天然串行，
        #   不可能出现两个计划同时跑。
        #   修 B1 之后计划转到**后台线程**执行 ⇒ 用户在计划进行中
        #   又说一句多步指令，就会**并发两个计划**：
        #     · 两套运动帧互相覆盖，车的实际轨迹不可预测；
        #     · 更要命的是下面 `_plan_abort.clear()` 会把**正在跑的那个计划**
        #       的中止信号抹掉 → 急停能失效。
        #   ⇒ 已在执行时**直接拒绝**（不排队、不覆盖）。要打断请先急停。
        #   用锁保护"检查并占位"，避免两个后台线程同时通过检查。
        #   注意：必须放在 `_plan_abort.clear()` **之前**，否则又会抹掉中止信号。
        with self._plan_lock:
            if self._plan_running:
                self.log("多步计划被拒绝：已有计划在执行中"
                         "（等它结束，或先急停中止）")
                return False, "已有计划在执行"
            self._plan_running = True      # 占位（下面 finally 会还原）

        try:
            return self._run_plan_locked(steps)
        finally:
            with self._plan_lock:
                self._plan_running = False

    def _run_plan_locked(self, steps):
        """`_run_plan` 的真正主体（已持有 `_plan_running` 占位）。"""
        n = len(steps)
        self._plan_abort.clear()      # 新计划开始：清掉上一次的残留中止信号
        self._plan_abort_by_estop = False
        reason = self._plan_not_alive_reason()
        if reason:
            self.log("多步计划未启动：%s" % reason)
            return False, reason

        # ② 计划期间关掉**逐步**动作播报（改由计划自己在首/尾各说一句）。
        #    注意：这是临时的，`finally` 里一定还原 —— 不能把用户的
        #    "动作播报"开关永久改掉。
        voice = bool(self._speak_enabled and self._announce_actions)
        old_announce = self._announce_actions
        self._announce_actions = False
        done = 0
        abort_reason = ""
        try:
            self.log("🧩 多步计划开始：%d 步（%s）"
                     % (n, "；".join(_plan_step_desc(s) for s in steps)))
            # ③ 先说计划摘要，并**等它播完**再动车（半双工，见 _wait_plan_speech）
            if voice:
                start = SPEAK_PLAN_START % _plan_num_cn(n)
                if self.speak(start, drop_pending=True):
                    self._wait_plan_speech()

            # ④ 逐步执行
            self._plan_total = n            # ★ 进度：总步数（网页 k/n 用）
            for i, step in enumerate(steps):
                reason = self._plan_not_alive_reason()
                if reason:
                    abort_reason = reason
                    break
                act = step.get("action")
                # ★ 进度：当前第几步（从 1 开始）+ 这一步在干什么
                self._plan_step = i + 1
                self._plan_label = ACTION_LABEL.get(act, act)
                if PLAN_SPEAK_STEPS and voice:
                    if self.speak(SPEAK_PLAN_STEP % num_to_cn(i + 1),
                                  drop_pending=True):
                        self._wait_plan_speech()
                ok, sub = self._do_action(
                    step, ACTION_LABEL.get(act, act),
                    prefix="计划第%d步：" % (i + 1))
                if not ok:
                    abort_reason = "第 %d 步下发失败（%s）" % (i + 1, sub)
                    self.log("⏹ %s —— 剩余 %d 步不再下发"
                             % (abort_reason, n - i - 1))
                    break
                done += 1
                # ★ 本步自己的 Timer 会在 duration 后停车；再留一段 settle 稳定
                wait_s = float(step.get("duration") or 0.0) + PLAN_STEP_SETTLE_SEC
                self.log("🧩 计划第 %d/%d 步已下发（等 %.2fs）"
                         % (i + 1, n, wait_s))
                if not self._plan_wait(wait_s):
                    abort_reason = (self._plan_not_alive_reason()
                                    or "已中止（剩余步骤丢弃）")
                    break

            # ⑤ 正常走完：补一次停车（Timer 可能还没触发/略晚），确保停稳再说话。
            #   ⚠️ 必须放在 try 里：此刻 `_announce_actions` 还是 False，
            #      stop() 不会额外播「已停止」把完成语挤掉。
            if not abort_reason and self._connected and self._car is not None:
                self.stop()
        finally:
            self._announce_actions = old_announce
            # _plan_running 由外层 _run_plan 的 finally 统一还原
            # ★ 进度归零（计划结束/中止后就不该再显示 k/n）
            self._plan_step = 0
            self._plan_total = 0
            self._plan_label = ""

        # ⑥ 收尾：所有播报都放在**运动全部结束之后**（运动中 aplay 必失败）
        if abort_reason:
            self.log("⏹ 多步计划已中止：已执行 %d/%d 步（%s）"
                     % (done, n, abort_reason))
            # 急停触发的中止**不再补"已中止"**：trigger_emergency 已经用 force
            # 投了「已急停」，再 drop_pending 一句会把那条顶掉，反而听不到急停确认。
            # `_plan_abort_by_estop` 标记在 `emergency_stop()` 里**先于** set 事件
            # 置位，所以这里读到的一定是对的（不会因为执行时序漏判）。
            if voice and not self._plan_abort_by_estop and not self._estop_locked:
                self.speak(SPEAK_PLAN_ABORT, drop_pending=True)
            return False, "计划中止：已执行 %d/%d 步（%s）" % (done, n, abort_reason)

        if voice:
            finish = SPEAK_PLAN_DONE % _plan_num_cn(n)
            self.speak(finish, drop_pending=True)
        self.log("✅ 多步计划完成：%d 步全部执行完" % done)
        return True, "计划完成：%d 步" % done

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
                out_desc = "无（板子外放与网络音箱都不可用）"
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
            "web_url": self.web_url(),      # 网络音箱地址（浏览器当喇叭时用它）
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
        # ★ 立刻叫停正在跑的多步计划（2026-10-17）：不能等它"下一步开始前"
        #   才发现 —— 那中间可能还有 duration+settle 的运动时间。
        #   `emergency_stop()` 里也 set 了一次（覆盖"不走本函数"的直接调用），
        #   这里再 set 是**幂等**的，且能表达"急停语义一定中止计划"。
        self._plan_abort.set()
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
        # ★ 顺带清掉多步计划的中止信号，否则解锁后计划类指令一直起不来。
        #   只有"没有计划正在执行"时才安全清（见 _plan_running 的说明）：
        #   万一出现"急停 → 计划还没察觉 → 人工点解锁"的极短时序，
        #   也不能把刚置上的中止信号抹掉，否则计划会继续跑。
        if not getattr(self, "_plan_running", False):
            self._plan_abort.clear()
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

        # ★ 唤醒词必须并进**同一份关键词表**（2026-10-12）：
        #   本板麦克风只有 1 个 subdevice → 只能有**一个**常驻 KWS 监听器，
        #   唤醒词不可能另起一路。所以这里把唤醒词追加到急停词表后面。
        #   唤醒功能关着时 merged 为 None → **行为与以前逐字一致**。
        merged = None
        want_merge = []
        if self._wake_enabled():
            try:
                want_merge = self._wake_keyword_lines(keywords_file)
                merged = self._wake_keywords_file(keywords_file)
            except Exception as e:
                self.log("合并唤醒词失败（忽略，用原词表）：%r" % (e,))
                merged = None
        if want_merge and not merged:
            # ★ 走这里说明**唤醒词没能进词表** —— 唤醒功能实际不生效。
            #   必须让用户看见（否则表现是"喊了没反应"，极难排查）。
            #   语音急停照旧用原词表启动，**安全能力不受影响**。
            warn = ("唤醒词未能并入 KWS 词表（%s）→ 唤醒功能不生效，"
                    "语音急停不受影响"
                    % "、".join(_wake_label_of(x) for x in want_merge))
            self.log("⚠ " + warn)
            self._voice_events.append(("wake_error", warn, time.time()))
        use_file = merged if merged else keywords_file

        def _worker():
            try:
                eng = _kws_engine.KwsEngine(
                    log=lambda s: self.log("[KWS] %s" % s))
                ok, msg = eng.load(keywords_file=use_file,
                                   threshold=threshold)
                if not ok and merged:
                    # 🔴 合并词表把**语音急停**带崩是不可接受的
                    #    （急停是安全能力，唤醒只是便利）→ 立刻退回原词表重试。
                    self.log("⚠ 合并了唤醒词的关键词表加载失败（%s），"
                             "退回原急停词表" % msg)
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

        分流（★ 安全关键，2026-10-12 加入唤醒词）：
          · **一级急停词**      → **原有逻辑一字不改**：记一笔 + 立即
            `trigger_emergency()`（同步停车、加锁）+ 投递播报。急停**永远优先**，
            绝不会因为某个词同时也是唤醒词而被分流掉。
          · **唤醒词**          → **只把任务投进唤醒队列，立即返回**。
            ⚠️ 绝不能在这里录音（5 秒）或播报（1 秒）——那样监听循环会卡住，
               这几秒内**语音急停彻底失效**，是安全问题不是性能问题。
            真正的活由常驻的 `_wake_worker` 线程慢慢做。
          · 两者都不命中        → 保持现状（当作急停词处理，与旧行为一致）。

        其余三件事（记一笔、立即急停、投递播报）与旧版逐字相同，见下方注释。
        """
        # ── 唤醒词分支（只入队，绝不阻塞监听线程）──────────────────────
        if self._is_wake_label(label) and not self._is_estop_label(label):
            if self._wake_enabled():
                self._enqueue_wake(label, ts)      # 内部只用 put_nowait + deque
            else:
                self.log("（唤醒词「%s」命中，但语音唤醒已关闭 → 忽略）" % label)
            return

        # ── 以下为原急停逻辑，**一字未改** ──────────────────────────────
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

    def _yield_mic(self, owner=None):
        """把麦克风从 KWS 监听手里要过来。返回 (是否有让位, 说明)。

        没有在监听时返回 (False, "") —— 调用方据此决定要不要恢复。

        ★ 2026-10-12：加 `_mic_busy` 互斥标记。本板麦克风只有 1 个 subdevice，
          两路录音（界面 PTT / 语音唤醒）同时来必然一路失败 ——
          已经在录音时**直接拒绝**，别去动 KWS（否则会把别人的麦克风弄丢）。

        ★ 2026-10-28：加 `owner` 与**长期占用**（`_mic_claim`）。原因：
          自动聆听是"反复开关麦克风"的循环，让位一次不够 —— 中间只要播报一次，
          `_restore_mic()` 就会把 KWS 拉起来抢走麦克风，下一句就录不进
          （真机症状：日志刷「麦克风被占用…稍后自动重试」，自动聆听永远用不了）。
        """
        cur = getattr(self, "_mic_claim", None)
        if cur and cur != owner:
            return False, "麦克风正被「%s」占用" % cur
        if getattr(self, "_mic_busy", False):
            # 陈旧标记自愈：既没人在录音、也没有占用者，说明上次异常没清干净
            if not getattr(self, "_mic_owned_by_record", False) and not cur:
                self.log("⚠ 麦克风占用标记是陈旧的（无人在录），已自动清除")
                self._mic_busy = False
            else:
                return False, "麦克风已在录音占用中（跳过重复让位）"
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
        self._mic_busy = True                 # ★ 标记：麦克风借出去了
        # ★ 关键：ALSA 释放不是瞬间的。实测 stop() 后立刻开录仍可能撞 -9999，
        #   所以这里主动等一小会儿（加上 Recorder 自己的退避重试，双保险）。
        time.sleep(0.35)
        self.log("🎤 录音需要麦克风，语音急停已临时让位（录完自动恢复）")
        return True, "语音急停已临时让位"

    def claim_mic(self, owner="record"):
        """**长期占用**麦克风，直到 `release_mic()`。返回 (是否拿到, 说明)。

        用于"反复开关录音设备"的场景（网页自动聆听）：让位一次是不够的。
        """
        ok, why = self._yield_mic(owner=owner)
        cur = getattr(self, "_mic_claim", None)
        if cur and cur != owner:
            return False, why or ("麦克风正被「%s」占用" % cur)
        self._mic_claim = owner
        self._mic_busy = True
        return ok, why

    def release_mic(self, owner=None):
        """释放 `claim_mic()` 的长期占用并把麦克风还给 KWS。

        `owner` 传了就校验（避免误释放别人的占用）。返回是否恢复了 KWS。
        """
        cur = getattr(self, "_mic_claim", None)
        if owner and cur and cur != owner:
            return False
        had = bool(cur)
        self._mic_claim = None
        self._mic_busy = False
        ok = self._restore_mic(True)
        if had and not ok:
            self.log("⚠ 已释放「%s」的麦克风占用，但 KWS 还没恢复成功"
                     % (owner or cur))
        return ok

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
        """播报后恢复 KWS 监听（它会自动重新打开麦克风）。

        ★ 2026-11-05 提速：原来这里固定 `time.sleep(0.25)` 再恢复，
          加上 KWS 侧 0.3s 起跳的指数退避，用户实测"播报完要等 3~5 秒"。
          现在改成**精确等声卡真的静下来**（去抖 20ms，最多 0.3s），
          一静下来立刻 resume；KWS 侧会用 20ms 快轮询去抢麦克风。
        """
        lis = self._kws
        if lis is None:
            return
        # ★ 录音正在用麦克风（用户在播报中途按了 F2 插话）→ 别抢回来，
        #   交给 PTT 那条路的 `_restore_mic()` 自己恢复
        if self._mic_owned_by_record:
            return
        t0 = time.time()
        try:
            # ★ 先等扬声器彻底排空再开麦（2026-10-11）：
            #   否则麦克风会收到自己的余音，若刚播的那句含
            #   「已急停/停车/刹车」就会**自我触发急停**
            #   （实测播报「停车」后 1 秒内命中「急停」，车被自己的声音锁死）。
            #   KWS 侧还有 `ECHO_GUARD_SECONDS` 二次兜底。
            # ★ 2026-11-05：不再盲等 0.25s，改成"确认静音就立刻走"（通常 20~40ms）
            self._wait_playback_quiet(max_wait=0.3)
            if lis.resume():
                self.log("🎤 播报结束，语音急停已恢复监听（判断静音+恢复用时 %.0f ms）"
                         % ((time.time() - t0) * 1000.0))
        except Exception as e:
            self.log("恢复 KWS 失败（忽略）：%r" % (e,))

    def _restore_mic(self, yielded):
        """录音结束后把麦克风还给 KWS 监听。返回是否恢复成功。

        🔴 **必须带重试预算**（2026-10-12 修）：
        旧实现失败一次就 `_kws_state="failed"` 并放弃 —— 而本板"暂时打不开麦克风"
        是**极常见的瞬时状态**（扬声器还在排空、ALSA 还没释放 subdevice），
        于是**语音急停会永久失效**，直到人工再点一次按钮。
        现在：`WAKE_RESTORE_TRIES` 次 × `WAKE_RESTORE_DELAY` 间隔，
        中途任何一次成功都立刻恢复；**全部失败**才置 failed 并记日志。

        注意：成功路径的日志**没有被吞掉**（第一次成功就是原来那句）。
        """
        if not yielded or not getattr(self, "_kws_yielded", False):
            return False
        # ★ 长期占用保护（2026-10-28）：自动聆听期间，播报结束也会走到这里，
        #   若无条件恢复就会把麦克风从自动聆听手里抢回来 → 下一句录不进。
        claim = getattr(self, "_mic_claim", None)
        if claim:
            self.log("🎤 麦克风正被「%s」占用，KWS 暂不恢复" % claim)
            return False
        self._kws_yielded = False
        self._mic_owned_by_record = False     # ★ 录音交还麦克风
        self._mic_busy = False
        lis = self._kws
        if lis is None:
            return False
        tries = max(1, int(WAKE_RESTORE_TRIES))
        last = ""
        for i in range(tries):
            try:
                ok, msg = lis.start()
            except Exception as e:
                ok, msg = False, "恢复异常：%r" % (e,)
            if ok:
                # 恢复成功 → 状态回 ready（旧实现若已置 failed，这里也能救回来）
                self._kws_state = "ready"
                self._kws_msg = msg or "监听中"
                if i == 0:
                    self.log("🎤 语音急停已恢复：%s" % self._kws_msg)
                else:
                    self.log("🎤 语音急停已恢复（第 %d/%d 次尝试成功）：%s"
                             % (i + 1, tries, self._kws_msg))
                return True
            last = msg or "未知原因"
            if i < tries - 1:
                self.log("⚠ 语音急停恢复第 %d/%d 次失败：%s（%.1fs 后重试）"
                         % (i + 1, tries, last, WAKE_RESTORE_DELAY))
                time.sleep(WAKE_RESTORE_DELAY)
        self._kws_state = "failed"
        self._kws_msg = "恢复失败（已重试 %d 次）：%s" % (tries, last)
        self.log("⚠ 语音急停恢复失败：%s（请重新点「开启语音急停」）"
                 % self._kws_msg)
        return False

    # ══════════════════════════════════════════════════════════════════
    #  语音唤醒（wake word）—— 2026-10-12 新增
    # ══════════════════════════════════════════════════════════════════
    #
    #  链路（严格按此顺序，顺序本身就是硬件约束）：
    #
    #     KWS 监听线程命中唤醒词
    #        └→ `_on_kws_hit`：**只入队**，立即返回（绝不阻塞监听线程）
    #             └→ `_wake_worker`（常驻 daemon）
    #                  └→ `_do_wake_session()`：
    #                       0) 急停锁定？→ 只提示，不碰麦克风，结束
    #                       1) `_yield_mic()`        让 KWS 交出麦克风
    #                       2) 播**预录提示音**（缺文件/播放失败 → 极短 TTS → 跳过）
    #                       3) 提示音**播完**才开录（半双工：播时不能录）
    #                          窗口 ≤5s，连续静音 0.8s 提前结束，可被 stop_event 取消
    #                       4) `_restore_mic()`      把麦克风还给 KWS（带重试预算）
    #                       5) ASR 识别 → `run_text_command()`（安全闸门不变）
    #                       6) 播报交给现有 SpeakQueue，**不重复播**
    #
    #  安全边界：唤醒会话期间语音急停短暂失效（麦克风被录音占着），
    #  所以 ① 只在"用户主动唤醒"时发生；② 录完立刻恢复；
    #  ③ 急停锁定时**根本不开录音**（那时车已停，保持急停链路可用为先）。

    # ── A. 唤醒词 / 开关 ────────────────────────────────────────────
    def _wake_words(self):
        """当前唤醒词列表（原始条目，可能含 `拼音 @标签` 的整行写法）。

        优先级：`set_wake_words()` → 环境变量 `CAR_WAKE_WORDS` → `DEFAULT_WAKE_WORDS`。
        🔴 与一级急停词**同名**的条目会被剔除（否则急停会被"唤醒"分流，
           那是最危险的一类配置错误）。
        """
        words = getattr(self, "_wake_word_list", None)
        if words:
            raw_list = list(words)
        else:
            raw = os.environ.get("CAR_WAKE_WORDS", "")
            raw_list = [p.strip() for p in raw.replace("，", ",").split(",")]
        estop = set(getattr(_kws_engine, "LEVEL1_KEYWORDS", ()) or ())
        out = []
        for w in raw_list:
            if not w:
                continue
            lab = _wake_label_of(w)
            if not lab or lab in estop:
                continue                     # 与急停词冲突 → 丢弃（保护急停）
            if w not in out:
                out.append(w)
        return out or [w for w in DEFAULT_WAKE_WORDS if w not in estop]

    def _wake_labels(self):
        """唤醒词**标签**列表（去掉拼音部分）。"""
        return [_wake_label_of(w) for w in self._wake_words()]

    def set_wake_words(self, words):
        """覆盖唤醒词表。`words` 可以是 `["小车车"]` 或 `"小车车,你好小车"`。

        返回 (是否成功, 提示)。传空 → 恢复成环境变量/默认值。
        """
        if words is None or (isinstance(words, str) and not words.strip()):
            self._wake_word_list = None
        elif isinstance(words, str):
            self._wake_word_list = [p.strip() for p in
                                    words.replace("，", ",").split(",") if p.strip()]
        else:
            self._wake_word_list = [str(w).strip() for w in words if str(w).strip()]
        labels = self._wake_labels()
        self._wake_msg = "唤醒词已设为：%s" % "、".join(labels)
        self.log(self._wake_msg)
        return bool(labels), self._wake_msg

    def _is_wake_label(self, label):
        """这个 KWS 命中标签是不是唤醒词。"""
        lab = (label or "").strip()
        return bool(lab) and lab in self._wake_labels()

    def _is_estop_label(self, label):
        """这个 KWS 命中标签是不是**一级急停词**。

        判据就是 `kws_engine.LEVEL1_KEYWORDS`（急停/紧急停止/小车急停/停车/刹车）。
        唤醒词在 `_wake_words()` 里已保证与它**不重合**。
        """
        lab = (label or "").strip()
        return bool(lab) and lab in set(
            getattr(_kws_engine, "LEVEL1_KEYWORDS", ()) or ())

    def _wake_enabled(self):
        """唤醒功能是否开启。

        运行期开关 `self._wake_on`（`enable_wake()` / `disable_wake()` 改它），
        **初始值**来自环境变量 `CAR_WAKE_ENABLE`（默认开，"0"/"off" 关）。
        """
        return bool(getattr(self, "_wake_on", True))

    def _wake_available(self):
        """唤醒链路能不能用。返回 (是否可用, 说明)。

        判据：① KWS 模块在；② ASR 模块在；③ 提示音有来源 ——
        预录 wav **或** 能现合成的 TTS（两者都没有时唤醒会是"哑的"，
        那种情况该报不可用，而不是让用户对着车喊半天没反应）。
        """
        if not HAS_KWS:
            return False, "缺少 kws_engine 模块：%s" % KWS_ERROR
        try:
            import asr_engine            # noqa: F401  （只探测，不加载模型）
        except Exception as e:
            return False, "缺少 asr_engine 模块：%s" % e
        if self._wake_prompt_path():
            return True, "可用"
        if self._tts is not None:
            return True, "可用（无预录提示音，唤醒时用极短 TTS 兜底）"
        return False, "没有预录提示音（%s/）且 TTS 未就绪" % self._wake_path

    # ── B. 唤醒词并进 KWS 关键词表 ──────────────────────────────────
    def _wake_keyword_lines(self, base_file=None):
        """把唤醒词转成 sherpa KWS 关键词行（`拼音 @标签`）。

        `base_file`：以哪份词表为基准去重（默认用 `kws_engine.DEFAULT_KEYWORDS_FILE`）。
        认不出拼音的词**直接报错跳过**，绝不瞎拼 —— 拼错只会静默不命中，
        比不写更让人困惑。
        """
        if not HAS_KWS:
            return []
        src = base_file or getattr(_kws_engine, "DEFAULT_KEYWORDS_FILE", "")
        known = set()
        if src:
            try:
                known = {lb for lb, _py in _kws_engine.load_keywords(src)}
            except Exception:
                known = set()
        lines = []
        for w in self._wake_words():
            lab = _wake_label_of(w)
            if lab in known:
                continue
            if "@" in w:
                lines.append(w)               # 调用方直接给了整行
                continue
            py = WAKE_WORD_PINYIN.get(lab)
            if not py:
                self.log("⚠ 唤醒词「%s」没有内置拼音，无法加入 KWS 词表"
                         "（可用 CAR_WAKE_WORDS=\"拼音 @词\" 显式指定）" % lab)
                continue
            lines.append("%s @%s" % (py, lab))
        return lines

    def _wake_kw_candidates(self):
        """合并词表的候选落盘路径（**按 uid 区分**，见下面的坑）。"""
        try:
            uid = os.getuid()
        except Exception:
            uid = 0
        out = []
        for d in (tempfile.gettempdir(), _HERE):
            for n in ("car_wake_keywords_%d.txt" % uid, "car_wake_keywords.txt"):
                p = os.path.join(d, n)
                if p not in out:
                    out.append(p)
        return out

    def _wake_keywords_file(self, base_file=None):
        """生成"急停词 + 唤醒词"的合并关键词文件，返回其路径。

        `None` = 不需要合并（没有唤醒词，或原表里已经有它们）。

        🔴 为什么必须合并（本板硬约束）：麦克风只有 1 个 capture subdevice，
           **同一时刻只能有一个 KWS 监听器** —— 唤醒词只能挤在急停词同一张表里，
           否则要么唤醒词永远听不到，要么第二个监听器必然 `-9999`。

        🔴 落盘为什么按 uid 起名 + 多路径重试（2026-10-14 板上实测踩到）：
           固定名 `/tmp/car_wake_keywords.txt` 一旦**被 root 建过**，
           普通用户就再也替换不掉它（`/tmp` 有 sticky 位，只有文件属主能删/改名）
           → `os.replace` 报 `PermissionError(1, 'Operation not permitted')`
           → **唤醒词被静默丢掉**（只留 5 个急停词），喊破嗓子也不响。
           现在：① 文件名带 uid，各用户互不干扰；② 逐个候选路径重试；
           ③ 记住上次成功的路径（`_wake_kw_file`）。全部失败才退回原词表
           （**语音急停优先于唤醒**，绝不因为唤醒把急停弄坏）。
        """
        if not self._wake_enabled():
            return None
        src = base_file or getattr(_kws_engine, "DEFAULT_KEYWORDS_FILE", "")
        try:
            with open(src, encoding="utf-8") as f:
                body = f.read()
        except Exception as e:
            self.log("读不到急停词表 %s：%r" % (src, e))
            return None
        extra = self._wake_keyword_lines(src)     # 以实际用的那份表去重
        if not extra:
            return None
        text = body.rstrip("\n") + "\n" + "\n".join(extra) + "\n"
        labels = "、".join(_wake_label_of(x) for x in extra)

        paths = []
        if self._wake_kw_file:
            paths.append(self._wake_kw_file)      # 上次成功的优先
        for p in self._wake_kw_candidates():
            if p not in paths:
                paths.append(p)

        last = None
        for out in paths:
            tmp = None
            try:
                with self._wake_lock:
                    tmp = "%s.tmp.%d" % (out, os.getpid())
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(text)
                    os.replace(tmp, out)      # 原子替换：KWS 读到的永远是完整文件
                self._wake_kw_file = out
                self.log("KWS 词表已并入唤醒词：%s（%s）" % (labels, out))
                return out
            except Exception as e:
                last = e
                if tmp:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass
        self.log("⚠ 写合并词表失败（唤醒词将听不到，语音急停不受影响）：%r" % (last,))
        return None

    # ── C. 预录提示音 ──────────────────────────────────────────────
    def _wake_prompt_path(self):
        """挑一个预录提示音文件。返回路径，找不到返回 ""。

        优先级：`CAR_WAKE_PROMPT`（可给绝对路径或文件名）→
                `wake_prompts/wake_zh.wav` → `wake_prompts/wake_beep.wav` → 目录里任意 .wav
        """
        env = (os.environ.get("CAR_WAKE_PROMPT") or "").strip()
        if env:
            p = env if os.path.isabs(env) else os.path.join(self._wake_path, env)
            if os.path.isfile(p):
                self._wake_prompt = p
                return p
            self.log("⚠ CAR_WAKE_PROMPT 指向的文件不存在：%s" % p)
        for name in (WAKE_PROMPT_FILE, WAKE_PROMPT_BEEP):
            p = os.path.join(self._wake_path, name)
            if os.path.isfile(p):
                self._wake_prompt = p
                return p
        try:
            for n in sorted(os.listdir(self._wake_path)):
                if n.lower().endswith(".wav"):
                    p = os.path.join(self._wake_path, n)
                    self._wake_prompt = p
                    return p
        except Exception:
            pass
        self._wake_prompt = ""
        return ""

    def _play_wav_file(self, path):
        """同步播放一个 wav 文件，返回 (是否成功, 说明)。

        优先走 `TtsEngine.play_samples()`（它会记住上次成功的 ALSA 设备）；
        TTS 引擎不可用时退回**直接 aplay** —— 因为本板 `aplay` **不带 `-D` 必失败**
        （ALSA 的 `default` 指向只有采集端点的 card0），所以必须先探测设备。
        """
        eng = self._tts
        if eng is not None:
            try:
                import wave
                import numpy as _np
                with wave.open(path, "rb") as f:
                    sr = f.getframerate()
                    n = f.getnframes()
                    raw = f.readframes(n)
                data = _np.frombuffer(raw, dtype=_np.int16).astype(_np.float32) / 32768.0
                if data.size:
                    ok, msg = eng.play_samples(sr, data)
                    return bool(ok), msg or ""
                return False, "wav 是空的"
            except Exception as e:
                self.log("预录提示音走 TTS 播放异常（改试 aplay）：%r" % (e,))

        import shutil
        import subprocess
        if not shutil.which("aplay"):
            return False, "系统里没有 aplay"
        devs = []
        if HAS_TTS:
            try:
                devs = [d["id"] for d in _tts_engine.output_probe()["alsa"]]
            except Exception:
                devs = []
        tried = []
        for dev in devs + [None]:
            cmd = ["aplay", "-q"] + (["-D", dev] if dev else []) + [path]
            try:
                p = subprocess.run(cmd, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   timeout=WAKE_PLAY_MAX_SEC)
            except Exception as e:
                tried.append("%r" % (e,))
                continue
            if p.returncode == 0:
                return True, "aplay%s" % ((" -D %s" % dev) if dev else "")
            tried.append((p.stdout or b"").decode("utf-8", "replace").strip()[:80])
        return False, "；".join(t for t in tried if t) or "aplay 播放失败"

    def _speak_now_sync(self, text):
        """**同步**合成并播放一句极短的话（提示音兜底用）。

        ⚠️ 与 `speak()` 的区别：`speak()` 是投队列、立刻返回；
           而提示音必须**播完才能开录音**（半双工），所以这里必须阻塞。
        """
        eng = self._tts
        if eng is None:
            return False, "TTS 未就绪"
        try:
            spk = 0 if self._tts_speaker is None else self._tts_speaker
            sr, data = eng.synth(text, spk)
        except Exception as e:
            return False, "合成异常：%r" % (e,)
        if data is None or not len(data):
            return False, "合成失败"
        try:
            ok, msg = eng.play_samples(sr, data)
            return bool(ok), msg or ""
        except Exception as e:
            return False, "播放异常：%r" % (e,)

    def _play_wake_prompt(self, cancel=None):
        """播唤醒提示音。返回 (是否播出, 说明)。**三级降级，绝不中断会话**。"""
        path = self._wake_prompt_path()
        if path and os.path.isfile(path):
            ok, msg = self._play_wav_file(path)
            if ok:
                return True, "预录提示音 %s（%s）" % (os.path.basename(path), msg)
            self.log("⚠ 预录提示音播放失败（%s）→ 改用极短 TTS「%s」"
                     % (msg, WAKE_PROMPT_TEXT))
        else:
            self.log("⚠ 没找到预录提示音（%s/）→ 改用极短 TTS「%s」"
                     % (self._wake_path, WAKE_PROMPT_TEXT))
        ok, msg = self._speak_now_sync(WAKE_PROMPT_TEXT)
        if ok:
            return True, "TTS 提示音「%s」（%s）" % (WAKE_PROMPT_TEXT, msg)
        self.log("⚠ 唤醒提示音都不可用（%s）→ 跳过提示音直接录音" % msg)
        return False, "无提示音（%s）" % msg

    def _wait_playback_quiet(self, max_wait=WAKE_QUIET_WAIT_SEC):
        """等声卡真正安静（提示音播完后才允许开麦克风）。返回是否安静。"""
        t0 = time.time()
        confirm = 0
        while time.time() - t0 < max_wait:
            if self.playback_idle():
                confirm += 1
                if confirm >= 2:
                    return True
            else:
                confirm = 0
            time.sleep(0.02)
        return False

    # ── D. 录音（静音提前结束 + 可取消）──────────────────────────────
    def _record_until_silence(self, cancel=None):
        """录一句话。返回 (是否成功, 采样率, 样本, 时长秒, 说明)。

        规则（需求原文）：窗口**最长 `WAKE_MAX_SEC`(5s)**，
        **连续静音 `WAKE_SILENCE_SEC`(0.8s) 提前结束**，
        全部用 `cancel.wait()` 而不是裸 `time.sleep()` —— **可被中途取消**。
        """
        try:
            import asr_engine as _AE
        except Exception as e:
            return False, 0, None, 0.0, "缺少 asr_engine：%r" % (e,)
        rec = _AE.Recorder()
        ok, msg = rec.start()
        if not ok:
            return False, 0, None, 0.0, "录音启动失败：%s" % msg

        t0 = time.time()
        speech = False
        silence_at = None
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    break
                el = time.time() - t0
                if el >= WAKE_MAX_SEC:
                    break
                lv = float(getattr(rec, "level", 0.0) or 0.0)
                now = time.time()
                if not speech:
                    if lv >= WAKE_START_LEVEL:
                        speech = True
                        silence_at = None
                    elif now - t0 >= WAKE_START_TIMEOUT:
                        break          # 一直没人说话，别白等满 5 秒
                else:
                    if lv < WAKE_SILENCE_LEVEL:
                        if silence_at is None:
                            silence_at = now
                        elif now - silence_at >= WAKE_SILENCE_SEC:
                            break      # ★ 静音 0.8s → 提前结束
                    else:
                        silence_at = None
                if cancel is not None:
                    cancel.wait(WAKE_POLL_SEC)
                else:
                    time.sleep(WAKE_POLL_SEC)
        finally:
            try:
                rate, samples = rec.stop()
            except Exception as e:
                rate, samples = 0, None
                self.log("停止录音异常：%r" % (e,))

        secs = (len(samples) / float(rate)) if (samples is not None and rate) else 0.0
        if not speech:
            return False, rate, samples, secs, "没听到说话（录了 %.1fs）" % secs
        if secs < WAKE_MIN_SEC:
            return False, rate, samples, secs, "说话太短（%.2fs）" % secs
        return True, rate, samples, secs, "录音 %.2fs @ %d Hz" % (secs, rate)

    # ── E. ASR（界面可注入；没注入就按需懒加载一次）───────────────────
    def set_asr_engine(self, eng):
        """把界面已经加载好的 ASR 引擎交给唤醒流程（**推荐**）。

        这样唤醒直接用现成模型，不必再加载一份 228MB 的 SenseVoice。
        返回 (是否成功, 提示)。
        """
        self._asr = eng
        self._asr_probe_done = True
        ready = bool(eng is not None and getattr(eng, "ready", False))
        self._asr_probe_note = "" if ready else "注入的 ASR 引擎尚未 load()"
        return ready, self._asr_probe_note or "ASR 已就绪"

    def _resolve_wake_asr(self):
        """拿到可用的 ASR 引擎；拿不到返回 None（原因写进 `_asr_probe_note`）。"""
        eng = getattr(self, "_asr", None)
        if eng is not None and getattr(eng, "ready", False):
            return eng
        with self._asr_lock:
            eng = getattr(self, "_asr", None)
            if eng is not None and getattr(eng, "ready", False):
                return eng
            if eng is None and self._asr_probe_done:
                return None                    # 试过了、没有，别反复加载
            try:
                import asr_engine as AE
            except Exception as e:
                self._asr_probe_note = "缺少 asr_engine 模块：%s" % e
                self._asr_probe_done = True
                self.log("唤醒不可用：%s" % self._asr_probe_note)
                return None
            if eng is None:
                eng = AE.AsrEngine(log=lambda s: self.log("[ASR] %s" % s))
                self._asr = eng
            ok, msg = eng.load()
            self._asr_probe_done = True
            if not ok:
                self._asr_probe_note = msg
                self.log("唤醒要用的 ASR 加载失败：%s" % msg)
                return None
            self.log("唤醒会话按需加载了 ASR 模型（%s）" % msg)
            return eng

    # ── F. 队列 / 工作线程 / 会话 ────────────────────────────────────
    def _enqueue_wake(self, label, ts=None):
        """把一次唤醒命中投进队列（**在 KWS 监听线程里调用，绝不阻塞**）。

        队列满了就丢**最旧**的一条：唤醒的语义是"要最新那一句"。
        """
        ts = time.time() if ts is None else ts
        self._wake_last_label = label
        # 立刻给界面一个反馈（deque.append 是 O(1) 且线程安全）
        try:
            self._voice_events.append(("wake_hit", label, ts))
        except Exception:
            pass
        try:
            self._wake_q.put_nowait((label, ts))
        except Exception:
            try:
                self._wake_q.get_nowait()
            except Exception:
                pass
            try:
                self._wake_q.put_nowait((label, ts))
            except Exception:
                pass
        return True

    def _ensure_wake_worker(self):
        """保证唤醒工作线程活着（幂等）。"""
        with self._wake_lock:
            t = getattr(self, "_wake_thread", None)
            if t is not None and t.is_alive():
                return t
            if getattr(self, "_wake_stop", None) is None:
                return None
            t = threading.Thread(target=self._wake_worker, name="wake-worker",
                                 daemon=True)
            self._wake_thread = t
            t.start()
            return t

    def _wake_worker(self):
        """常驻唤醒工作线程：取任务 → `_do_wake_session()`。"""
        while not self._wake_stop.is_set():
            try:
                item = self._wake_q.get(timeout=0.5)
            except Empty:
                continue
            except Exception:
                time.sleep(0.1)
                continue
            if item is None:
                continue
            label, ts = item
            try:
                self._do_wake_session(label=label, hit_ts=ts)
            except Exception as e:
                self._wake_fail("唤醒会话异常：%r" % (e,))

    def cancel_wake(self):
        """取消正在进行的唤醒会话（录音/识别中途也能停）。返回是否发了取消。"""
        ev = getattr(self, "_wake_abort", None)
        if ev is None:
            return False
        try:
            ev.set()
        except Exception:
            return False
        return True

    def _wake_fail(self, msg):
        """记录一次唤醒失败（状态 + 日志 + `wake_error` 事件）。"""
        self._wake_state = "error"
        self._wake_msg = msg
        self.log("⚠ 唤醒失败：%s" % msg)
        try:
            self._voice_events.append(("wake_error", msg, time.time()))
        except Exception:
            pass
        return False, msg

    def _do_wake_session(self, label=None, hit_ts=None, stop_event=None, asr=None):
        """跑完一次唤醒会话。返回 (是否走通指令链路, 说明)。

        ⚠️ 只能在**唤醒工作线程**里调用（或测试直接调）。**绝不能**在
           `_on_kws_hit`（KWS 监听线程）里调用 —— 那会卡住语音急停。

        这里只做"上锁 / 解锁"：真正的步骤在 `_wake_session_inner()`，
        这样**任何一条提前返回**（取消 / 急停锁定 / 录音失败…）都会被 finally
        兜住，不会留下"永远 busy"的假状态。
        """
        t_hit = hit_ts or time.time()
        cancel = stop_event if stop_event is not None else threading.Event()
        self._wake_abort = cancel
        self._wake_busy = True
        self._wake_last_open_cost = 0.0
        try:
            return self._wake_session_inner(label, t_hit, cancel, asr)
        finally:
            self._wake_busy = False
            if self._wake_abort is cancel:
                self._wake_abort = None
            if self._wake_state in ("prompt", "recording",
                                    "recognizing", "executing"):
                self._wake_state = "listening"

    def _wake_session_inner(self, label, t_hit, cancel, asr):
        """唤醒会话的主体（步骤 0~6，顺序即硬件约束，见本节开头注释）。"""
        labels = self._wake_labels()
        label = label or (labels[0] if labels else "?")
        self._wake_last_label = label
        self.log("🔔 唤醒命中「%s」→ 开始唤醒会话" % label)

        # 0) 急停锁定 → 只提示，**不开录音**（车已停，先保住急停链路）
        if self._estop_locked:
            msg = "急停已锁定（%s），请先解除再唤醒" % (self._estop_reason or "未知")
            self._wake_state = "locked"
            self._wake_msg = msg
            self.log(msg)
            self.speak("急停已锁定，请先解除", drop_pending=True, force=True)
            self._voice_events.append(("wake_error", msg, time.time()))
            return False, msg

        # 0b) ASR 必须先就绪：没它就完全不必折腾麦克风
        eng = asr if asr is not None else self._resolve_wake_asr()
        if eng is None:
            return self._wake_fail("语音识别未就绪：%s"
                                   % (self._asr_probe_note or "ASR 不可用"))

        rate, samples, secs, note = 0, None, 0.0, ""
        rec_ok = False
        yielded = False
        try:
            # 1) 让 KWS 交出麦克风（本板只有 1 个 capture subdevice）
            self._wake_state = "prompt"
            self._wake_msg = "正在准备听你说话…"
            yielded, why = self._yield_mic()
            if why:
                self.log("🎤 %s" % why)
            self._mic_busy = True          # KWS 没在跑时 _yield_mic 返回 False

            # 1b) 让扬声器让位：丢掉过期播报，等声卡安静（提示音才听得清）
            if not self.playback_idle():
                quiet, why2 = self.yield_speaker(max_wait=2.0)
                if why2:
                    self.log("🔊 唤醒提示音前：%s" % why2)

            # 2) 预录提示音（失败回退极短 TTS；都失败就跳过，绝不中断）
            ok, how = self._play_wake_prompt(cancel)
            self.log("🔔 唤醒提示音：%s" % how)
            if ok:
                self._wait_playback_quiet()
            if cancel.is_set():
                self._wake_state = "listening"
                self._wake_msg = "唤醒已取消"
                return False, "已取消"

            # 3) 提示音**播完才**开录（半双工：播时不能录）
            self._wake_state = "recording"
            self._wake_msg = "正在听你说…"
            self._wake_last_rec_start = time.time() - t_hit   # 唤醒→开录音 耗时
            rec_ok, rate, samples, secs, note = self._record_until_silence(cancel)
            self._wake_last_open_cost = time.time() - t_hit
            self._wake_last_record_sec = secs
            self.log("⏱ 唤醒→开录音 %.2fs；录音 %.2fs（%s）"
                     % (self._wake_last_rec_start, secs, note))
        finally:
            # 4) 无论成败，麦克风一定还给 KWS（安全第一）
            self._restore_mic(yielded)
            self._mic_busy = False

        if not rec_ok:
            if cancel.is_set():
                self._wake_state = "listening"
                self._wake_msg = "唤醒已取消"
                return False, "已取消"
            return self._wake_fail(note or "没听清")

        # ★★ 取消闸门（2026-10-14 修 BUG-1，**必须放在 rec_ok 判定之后、ASR 之前**）
        #
        # 🔴 原来只在 `if not rec_ok:` 分支里查取消，而**取消恰恰会让录音
        #    "提前正常结束"**（`_record_until_silence` 看到 stop_event 就收工返回
        #    `rec_ok=True`）→ 于是**跳过取消检查**，继续 ASR 并
        #    `run_text_command()` **真的把车动起来**。
        #    实测（假件复现）：`cancel_wake()` 之后 500ms 仍打印
        #    `run_text_command 前进` 并下发 `(300,0,0)` 运动帧。
        #
        # 为什么这条很严重：`cancel_wake()` 不只是界面按钮 ——
        # `disable_wake()`（用户关掉唤醒）与 `shutdown_wake()`（退出程序）
        # **都会调它**。所以"关掉了还照样动"是完全可能的。
        # ⇒ 取消语义必须是：**已经录到的音频也不再识别、不再执行**。
        if cancel.is_set():
            self._wake_state = "listening"
            self._wake_msg = "唤醒已取消"
            self.log("🔔 唤醒已取消 → 丢弃已录音频，不识别不执行")
            return False, "已取消"

        # 5) ASR → 现有指令链路（规则优先 → LLM 兜底，安全闸门不变）
        self._wake_state = "recognizing"
        self._wake_msg = "正在识别…"
        t0 = time.time()
        ok, res = eng.recognize(rate, samples)
        self._wake_last_asr_sec = time.time() - t0
        if not ok:
            return self._wake_fail("识别失败：%s" % res)

        text = str(res).strip()
        self._wake_text = text
        self._wake_ts = time.time()
        self._wake_count += 1
        self._voice_events.append(("wake_text", text, self._wake_ts))
        self.log("🔔 唤醒识别：「%s」（录音 %.2fs，ASR %.2fs，唤醒→录完 %.2fs）"
                 % (text, secs, self._wake_last_asr_sec, self._wake_last_open_cost))

        # 6) 播报结果由现有 SpeakQueue 负责，这里**不重复播**
        self._wake_state = "executing"
        ok2, msg2 = self.run_text_command(text)
        self._wake_msg = "已识别「%s」→ %s" % (text, msg2)
        self.log("🔔 唤醒指令链路：%s（%s）"
                 % ("已执行" if ok2 else "未执行", msg2))
        return ok2, msg2

    # ── G. 界面接口（签名固定，勿改）────────────────────────────────
    def wake_status(self):
        """唤醒状态快照（界面定时器调用）。签名与键名是**界面契约**。"""
        avail, amsg = self._wake_available()
        prompt = self._wake_prompt_path()
        return {
            "enabled": self._wake_enabled(),
            "available": bool(avail),
            "busy": bool(self._wake_busy),
            "state": self._wake_state,
            "msg": self._wake_msg or amsg,
            "prompt": prompt or "",
            "last_text": self._wake_text,
            "last_ts": self._wake_ts,
            "count": self._wake_count,
            "words": self._wake_labels(),     # 附带信息，界面可显示当前唤醒词
        }

    def enable_wake(self, enable_kws=True):
        """开启语音唤醒。返回 (是否成功, 提示)。

        `enable_kws=True`（默认）时，如果语音急停监听还没起来，会**顺带把它拉起来** ——
        唤醒词靠的就是这个监听器（本板只有一个麦克风，不可能另起一路）。
        不想要这个副作用就传 `enable_kws=False`。
        """
        avail, amsg = self._wake_available()
        if not avail:
            self._wake_state = "error"
            self._wake_msg = amsg
            self.log("语音唤醒不可用：%s" % amsg)
            self._voice_events.append(("wake_error", amsg, time.time()))
            return False, amsg

        # 与一级急停词同名的唤醒词会被 `_wake_words()` 静默剔除 → 在这里明说
        raw = self._wake_words()
        estop_cfg = set(getattr(_kws_engine, "LEVEL1_KEYWORDS", ()) or ())
        dropped = []
        if getattr(self, "_wake_word_list", None):
            dropped = [w for w in self._wake_word_list
                       if _wake_label_of(w) in estop_cfg]
        elif os.environ.get("CAR_WAKE_WORDS"):
            dropped = [p.strip() for p in
                       os.environ.get("CAR_WAKE_WORDS", "").replace("，", ",").split(",")
                       if p.strip() and _wake_label_of(p.strip()) in estop_cfg]
        if dropped:
            self.log("⚠ 这些唤醒词与一级急停词重合，已忽略（保护急停）：%s"
                     % "、".join(_wake_label_of(w) for w in dropped))

        self._wake_on = True
        self._ensure_wake_worker()
        prompt = self._wake_prompt_path()
        labels = self._wake_labels()
        msg = "语音唤醒已开启（唤醒词：%s）" % ("、".join(labels) or "无")
        if not prompt:
            msg += "；没有预录提示音，将用极短 TTS 兜底"
            self.log("⚠ 没有预录提示音（%s/ 为空）→ 唤醒时用极短 TTS「%s」兜底"
                     % (self._wake_path, WAKE_PROMPT_TEXT))
        else:
            self.log("唤醒预录提示音：%s" % prompt)

        if enable_kws:
            lis = self._kws
            running = bool(lis is not None and getattr(lis, "running", False))
            if not running:
                was_loading = (self._kws_state == "loading")
                ok, km = self.enable_voice_emergency(background=True)
                if not ok:
                    msg += "；⚠ 语音急停监听启动失败（%s）" % km
                    self._wake_state = "error"
                    self._wake_msg = msg
                    self.log(msg)
                    self._voice_events.append(("wake_error", km, time.time()))
                    return False, msg
                msg += ("；语音急停监听正在加载（唤醒词靠它听）" if was_loading
                        else "；已同时启动语音急停监听（唤醒词靠它听）")
        self._wake_state = "listening"
        self._wake_msg = msg
        self._wake_prompt = prompt
        self.log(msg)
        return True, msg

    def disable_wake(self):
        """关闭语音唤醒（**不影响语音急停**，监听继续跑）。返回 (True, 提示)。"""
        self._wake_on = False
        self.cancel_wake()
        self._wake_state = "off"
        self._wake_msg = "语音唤醒已关闭（语音急停不受影响，仍在监听）"
        self.log(self._wake_msg)
        return True, self._wake_msg

    def shutdown_wake(self):
        """退出程序时停掉唤醒工作线程（`shutdown_voice()` 会调）。"""
        self._wake_on = False
        self.cancel_wake()
        try:
            self._wake_stop.set()
        except Exception:
            pass
        self._wake_state = "off"
        self._wake_msg = "已停止"

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
        try:
            self.shutdown_wake()          # 先停唤醒工作线程，别再录/播
        except Exception:
            pass
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
            # ── 多步计划进度（网页对话卡片显示 k/n）──
            "plan_running": bool(getattr(self, "_plan_running", False)),
            "plan_step": int(getattr(self, "_plan_step", 0) or 0),
            "plan_total": int(getattr(self, "_plan_total", 0) or 0),
            "plan_label": getattr(self, "_plan_label", "") or "",
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
