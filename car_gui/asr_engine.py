# -*- coding: utf-8 -*-
"""
asr_engine.py —— 离线语音识别引擎（sherpa-onnx + SenseVoice）

职责
----
把「麦克风采集 → 断句 → 语音转文字」这一整套封在一个类里，**不依赖任何 GUI 库**，
供 car_gui_pyside.py / car_gui_tk.py / asr_cli.py 共同使用。

设计要点
--------
1. **依赖全部懒加载**：没装 sherpa-onnx / sounddevice / numpy 时，本模块仍可正常
   import，只是 AsrEngine.load() 会返回明确的错误提示。这样"没有语音库也能手动控车"。
2. **两种聆听模式**
   - `listen_ptt_*()`：按住说话（Push-to-Talk），不依赖断句算法，最可靠
   - `listen_auto()`：按一次开始，检测到静音自动结束，说话人不用按键
3. **断句优先用 Silero VAD**（sherpa-onnx 自带、0.6MB），加载失败自动退回
   自研的能量端点检测（纯 numpy），保证"永远有一个能用"。
4. **采样率自适应**：优先以 16kHz 打开麦克风；失败就用设备默认采样率
   （如 44.1kHz），ASR 侧由 sherpa-onnx 内部重采样，VAD 侧我们自己重采样到 16k。

模型
----
    sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17
      ├── model.int8.onnx   (约 228 MB)
      └── tokens.txt        (约 308 KB)
    silero_vad.onnx         (约 0.6 MB，可选，用于自动断句)

下载见 download_asr_model.sh。

自测（不需要 GUI）：
    python3 asr_engine.py --status
    python3 asr_engine.py --list-devices
    python3 asr_engine.py --model-dir ~/models/sense-voice --file test.wav
"""

import json
import os
import re
import sys
import threading
import time

# ── 可选依赖：全部懒加载，缺了也不让 import 失败 ─────────────────────────
try:
    import numpy as _np
    HAS_NUMPY = True
except Exception:
    _np = None
    HAS_NUMPY = False

try:
    import sounddevice as _sd
    HAS_SOUNDDEVICE = True
except Exception:
    _sd = None
    HAS_SOUNDDEVICE = False

_HAS_SHERPA = False
_SHERPA_ERROR = ""
try:
    import sherpa_onnx as _sherpa
    _HAS_SHERPA = True
except Exception as _e:                      # pragma: no cover
    _sherpa = None
    _SHERPA_ERROR = str(_e)


# 各可选依赖「缺了怎么办」——用于把 ModuleNotFoundError 变成可执行指引
# 值 = (安装动作, 附加 pip 源)；安装动作里用 %s 占位当前解释器路径
_HOWTO = {
    "numpy": ("pip:%s -m pip install -U numpy",
              "-i https://pypi.tuna.tsinghua.edu.cn/simple"),
    "sounddevice": ("apt:libportaudio2", None),
    "sherpa_onnx": ("pip:%s -m pip install -U sherpa-onnx",
                    "-i https://pypi.tuna.tsinghua.edu.cn/simple"),
}


def find_compatible_python():
    """
    在本板找另一个「装了语音依赖」的 python3。

    板子上通常同时有系统 python3 与 conda 环境，依赖只装在其中一套。
    你在错的那套里跑脚本就会看到「缺少 sounddevice」。这里扫一遍给出备选。
    找不到返回 []。
    """
    import glob as _glob
    import subprocess as _sp

    probe = ("import importlib;"
             "print(''.join('1' if importlib.util.find_spec(m) else '0' "
             "for m in ('serial','numpy','sounddevice','sherpa_onnx')))")

    cands = ["/usr/bin/python3", "/usr/local/bin/python3",
             # ⚠️ 本板实测 conda 装在 /usr/local/miniconda3，且 .bashrc 有 conda init
             #    → 交互登录自动激活 base（3.9，没有 voice 依赖）。必须覆盖到。
             "/usr/local/miniconda3/bin/python3",
             "/usr/local/anaconda3/bin/python3",
             "/usr/local/conda/bin/python3",
             "/opt/miniconda3/bin/python3",
             "/opt/anaconda3/bin/python3",
             "/opt/conda/bin/python3",
             os.path.expanduser("~/miniconda3/bin/python3"),
             os.path.expanduser("~/anaconda3/bin/python3")]
    for root in ("/usr/local/miniconda3", "/usr/local/anaconda3",
                 "/usr/local/conda", "/opt/miniconda3", "/opt/anaconda3",
                 "/opt/conda", os.path.expanduser("~/miniconda3"),
                 os.path.expanduser("~/anaconda3")):
        cands.extend(sorted(_glob.glob(os.path.join(root, "envs/*/bin/python3"))))

    out = []
    for p in cands:
        if not os.path.exists(p) or os.path.realpath(p) == os.path.realpath(
                getattr(sys, "executable", "")):
            continue
        try:
            r = _sp.run([p, "-c", probe], capture_output=True, text=True,
                        timeout=20)
            bits = r.stdout.strip()
            if r.returncode != 0 or len(bits) != 4:
                continue
        except Exception:
            continue
        mods = dict(zip(("serial", "numpy", "sounddevice", "sherpa_onnx"), bits))
        if mods["sounddevice"] == "1":
            out.append({"path": p, "mods": mods,
                        "voice": mods["sounddevice"] == "1" and mods["sherpa_onnx"] == "1"})
    out.sort(key=lambda d: (not d["voice"], d["path"]))
    return out


def dependency_hint(need=("numpy", "sounddevice")):
    """
    生成「缺依赖」时的完整人话指引（含本板换解释器的建议）。

    用法：
        ok, hint = E.dependency_hint(["sounddevice"])
        if not ok: print(hint)
    返回 (是否齐全, 文本)。
    """
    have = {"numpy": HAS_NUMPY, "sounddevice": HAS_SOUNDDEVICE,
            "sherpa_onnx": _HAS_SHERPA}
    missing = [m for m in need if not have.get(m)]
    if not missing:
        return True, ""

    lines = []
    lines.append("  ⚠️  当前解释器缺少：%s" % "、".join(missing))
    lines.append("     解释器 = %s" % sys.executable)
    lines.append("     版本   = %d.%d.%d" % sys.version_info[:3])
    if os.environ.get("CONDA_PREFIX"):
        lines.append("     conda  = %s" % os.environ["CONDA_PREFIX"])
    lines.append("")
    lines.append("  这条错误 90% 是「用错了那套 Python」——本板同时装了多套")
    lines.append("  Python，语音依赖只装在其中一套里。请按下面顺序处理：")
    lines.append("")
    lines.append("  【第 1 步】看看别的解释器能不能用：")
    lines.append("      python3 env_doctor.py          # 在 ~/car_gui 下运行")
    others = find_compatible_python()
    if others:
        lines.append("    已经扫到可用的：")
        for d in others[:4]:
            tag = "（语音链路完整）" if d["voice"] else "（只有部分依赖）"
            lines.append("        %s  %s" % (d["path"], tag))
        lines.append("    → 直接用它的绝对路径跑：")
        lines.append("        %s kws_record.py" % others[0]["path"])
    else:
        lines.append("    没扫到别的可用解释器 → 走第 2 步装依赖。")
    lines.append("")
    lines.append("  【第 2 步】在当前解释器里补装依赖：")
    _PIP_INDEX = "-i https://pypi.tuna.tsinghua.edu.cn/simple"
    for m in missing:
        entry = _HOWTO.get(m)
        if entry is None:
            # 未知依赖：给个通用 pip 写法
            lines.append("      %s -m pip install -U %s %s"
                         % (sys.executable, m, _PIP_INDEX))
            continue
        how, index = entry
        if how.startswith("apt:"):
            # 底层系统库走 apt，再单独装 python 包
            lines.append("      sudo apt install -y %s" % how[4:])
            lines.append("      %s -m pip install -U %s %s"
                         % (sys.executable, m, _PIP_INDEX))
        else:
            # how 形如 "pip:%s -m pip install -U xxx"，%s 换成真实解释器
            cmd = (how[4:] if how.startswith("pip:") else how) % sys.executable
            lines.append("      %s %s" % (cmd, index or _PIP_INDEX))
    lines.append("    或一键（含所有语音依赖）：")
    lines.append("      bash install_deps.sh")
    lines.append("")
    lines.append("  【第 3 步】如果想固定下来，之后所有脚本自动跟着走：")
    lines.append("      <可用解释器路径> env_doctor.py --write")
    return False, "\n".join(lines)


# ── 默认路径与参数 ──────────────────────────────────────────────────────
DEFAULT_MODEL_DIRS = (
    os.path.expanduser("~/models/sense-voice"),
    "/data/models/sense-voice",
    "/home/HwHiAiUser/models/sense-voice",
)

MODEL_FILE_CANDIDATES = ("model.int8.onnx", "model.onnx")
TOKENS_FILE = "tokens.txt"
SILERO_VAD_FILE = "silero_vad.onnx"

ASR_SAMPLE_RATE = 16000        # SenseVoice 的特征采样率
DEFAULT_THREADS = 2            # 板子只有 4 核，留给 GUI 和串口 2 核
DEFAULT_LANGUAGE = "zh"        # 锁定中文，避免 auto 误判

# 断句参数
PTT_MIN_SECONDS = 0.3          # 短于此判为误按
AUTO_TIMEOUT = 8.0             # 一直没说话就退出（秒）
AUTO_MAX_SECONDS = 12.0        # 单句最长（秒）
AUTO_MIN_SPEECH = 0.25         # 有效语音最短时长（秒）
AUTO_TAIL_SILENCE = 0.8        # 说完后静音多久算结束（秒）


def guess_model_dir():
    """按常见位置找一个存在 model.int8.onnx 的模型目录。"""
    for d in DEFAULT_MODEL_DIRS:
        if find_model_files(d)[0]:
            return d
    for d in DEFAULT_MODEL_DIRS:
        if os.path.isdir(d):
            return d
    return DEFAULT_MODEL_DIRS[0]


def find_model_files(model_dir):
    """在 model_dir 里找 (模型文件路径, tokens 路径)；找不到返回 (None, None)。"""
    if not model_dir or not os.path.isdir(model_dir):
        return None, None
    model = None
    for name in MODEL_FILE_CANDIDATES:
        p = os.path.join(model_dir, name)
        if os.path.isfile(p):
            model = p
            break
    if model is None:
        # 退一步：目录里任意 *.onnx（排除 vad）
        for f in sorted(os.listdir(model_dir)):
            if f.endswith(".onnx") and "vad" not in f.lower():
                model = os.path.join(model_dir, f)
                break
    tokens = os.path.join(model_dir, TOKENS_FILE)
    if not os.path.isfile(tokens):
        tokens = None
    return model, tokens


def dependency_status():
    """返回各依赖的可用状态，给界面/自检脚本用。"""
    st = {
        "numpy": HAS_NUMPY,
        "sounddevice": HAS_SOUNDDEVICE,
        "sherpa_onnx": _HAS_SHERPA,
        "sherpa_error": _SHERPA_ERROR,
        "versions": {},
        "portaudio": "",
    }
    if HAS_NUMPY:
        st["versions"]["numpy"] = getattr(_np, "__version__", "?")
    if HAS_SOUNDDEVICE:
        st["versions"]["sounddevice"] = getattr(_sd, "__version__", "?")
        try:
            st["portaudio"] = _sd.get_portaudio_version()[1]
        except Exception:
            st["portaudio"] = "查询失败"
    if _HAS_SHERPA:
        st["versions"]["sherpa_onnx"] = getattr(_sherpa, "__version__", "?")
    return st


def list_input_devices():
    """列出可用录音设备。返回 [{'index','name','samplerate','channels'}, ...]"""
    out = []
    if not HAS_SOUNDDEVICE:
        return out
    try:
        devices = _sd.query_devices()
    except Exception:
        return out
    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            out.append({
                "index": i,
                "name": d.get("name", "?"),
                "samplerate": int(d.get("default_samplerate", 0) or 0),
                "channels": int(d.get("max_input_channels", 0)),
                #: 播放通道数 —— 打分要用（见 `score_input_device`）：
                #: 纯采集（out=0）多半是真麦克风；采集=播放的多半是"音箱卡"。
                "out_channels": int(d.get("max_output_channels", 0) or 0),
            })
    return out


# 「真实硬件」的判据：名字里带 hw: / 具体芯片型号。
# ALSA 软件插件（sysdefault / spdif / samplerate / speexrate / upmix /
# vdownmix / default / pulse）都不是真硬件，ch 数是 128/32/8/6 这类假值。
_PLUGIN_MARKERS = ("sysdefault", "spdif", "samplerate", "speexrate",
                   "upmix", "vdownmix", "null", "pulse", "default")


def is_real_device(name):
    """
    判断一个设备名是否是真实录音硬件（而不是 ALSA/Pulse 软件插件）。

    真实硬件长这样：
        USB PnP Sound Device: Audio (hw:0,0)
    软件插件长这样：
        sysdefault / spdif / samplerate / pulse / default …
    """
    if not name:
        return False
    low = name.lower()
    if "(hw:" in low or "hw:" in low:
        return True
    # 名字里带 USB 且不是 default/pulse 这类纯插件名
    if "usb" in low and not any(m in low for m in _PLUGIN_MARKERS):
        return True
    return False


def real_input_devices():
    """只返回真实硬件设备。"""
    return [d for d in list_input_devices() if is_real_device(d["name"])]


def device_identity(name):
    """把一个设备名归一化成**稳定标识**，用于跨重启记忆用户的选择。

    ⚠️ 为什么不能用 sounddevice 的**索引**做标识（2026-10-08 踩到）：
        本板插拔 USB 声卡后索引会重排 —— 实测 `PCM2902` 从 `hw:0,0`
        变成了 `hw:1,0`，`SPACETOUCH` 从 `hw:1,0` 变成了 `hw:0,0`。
        拿索引当 ID 会"记住错的设备"。

    ⚠️ 也不能直接用整个名字：`USB PnP Sound Device: Audio (hw:1,0)`
        里的 `hw:1,0` 会随插拔变化。

    ⚠️ **还要剥掉 GUI 下拉框的 `[0] ` 前缀**（2026-10-09 踩到）：
        GUI 里显示的是 `[0] USB Audio: - (hw:0,0)`，直接把 itemText 传进来
        会存成 `[0] usb audio:` —— 索引和型号混在一起，
        既不可读，也容易和别的东西撞。
    ⇒ 去掉 `[N] ` 前缀 + 去掉 `(hw:N,M)` 尾巴，只留设备型号部分。
    """
    if not name:
        return ""
    s = name.strip()
    # 剥掉 GUI 的 "[0] " / "[12] " 前缀
    if s.startswith("["):
        j = s.find("]")
        if 0 < j <= 4:                      # "[0]"~"[999]" 都算
            s = s[j + 1:].strip()
    # 切掉 " (hw:1,0)" / ": - (hw:1,0)" 这类尾巴
    i = s.find("(hw:")
    if i > 0:
        s = s[:i]
    return s.strip().rstrip(":").rstrip("-").strip().lower()


def score_input_device(d):
    """给一个输入设备打分，**分数越高越可能是"真正的麦克风"**。

    🔴 为什么需要打分（2026-10-08 用户实测报的 bug）：
        本板有两块 USB 音频设备，原来的逻辑是"名字里含 usb 的第一个"，
        于是选中了 `SPACETOUCH USB Audio`（**它是音箱卡，采集端没有麦**）
        → 录到的全是底噪（RMS 0.0004）→ ASR 只能识别出"。"。
        而真正的麦克风是 `USB PnP Sound Device`（纯采集）
        —— **两块名字里都含 "USB"，靠名字根本区分不了**。

    判据（按可靠性排序）：
      · **采集/播放不对称**最可靠：纯采集（out=0）几乎一定是麦克风；
        采集=播放的"音箱卡"最可疑（它的输入往往是线路输入或干脆没有）。
      · **单声道**更像语音麦克风；2 声道可能是立体声线路输入。
      · 名字里带 mic / 麦克风 加分（弱信号，只作微调）。
    """
    ch_in = int(d.get("channels", 0) or 0)
    ch_out = int(d.get("out_channels", 0) or 0)
    name = (d.get("name") or "").lower()

    score = 0
    # ① 纯采集设备（无播放能力）→ 最像麦克风
    if ch_out == 0:
        score += 100
    else:
        # ② 有播放能力：如果采集/播放数量一样，多半是"音箱卡"
        if ch_in == ch_out:
            score -= 60
        else:
            score -= 20
    # ③ 单声道更像语音麦
    if ch_in == 1:
        score += 30
    elif ch_in == 2:
        score += 10
    # ④ 名字线索（弱信号）
    if "mic" in name or "麦克风" in name:
        score += 25
    if "pnp" in name or "codec" in name:
        score += 10
    # ⑤ 音频接口/声卡类名字通常不是麦克风
    for bad in ("audio", "speaker", "headphone", "hdmi"):
        if bad in name:
            score -= 15
    return score


def pick_input_device(devices=None, prefer="usb"):
    """从候选里**按能力打分**挑最像麦克风的那个。返回设备 dict 或 None。"""
    devs = real_input_devices() if devices is None else devices
    if not devs:
        return None
    cands = devs
    if prefer:
        p = [d for d in devs if prefer.lower() in (d.get("name") or "").lower()]
        if p:
            cands = p
    # 分数相同时，**保持枚举顺序**（稳定，不随机跳）
    return max(cands, key=lambda d: score_input_device(d))


def guess_input_device(prefer="usb"):
    """
    挑一个最像 USB 麦克风的输入设备，找不到就返回系统默认。

    选择优先级（重要，2026-10-08 重构）：
        1. **用户上次手动选择的设备**（按 `device_identity` 记住，见
           `save_preferred_input` / `_load_preferred_input`）
        2. 名字里带 prefer（默认 "usb"）的真实硬件里，**按能力打分**最高的
        3. 任意真实硬件里打分最高的
        4. sounddevice 的默认输入设备（可能 -1，会返回 None）

    ⚠️ 为什么强调「真实硬件」？
        PulseAudio 运行时，ALSA 会额外暴露 `pulse` / `default` 这类虚拟插件。
        它们的 ch 数是 32/128 的假值，直接选它有时能录、有时报
        `Error querying device -1`。所以**永远优先选带 hw: 的真硬件**。

    ⚠️ 找不到任何设备时返回 **None**（不是 -1），
        调用方必须能处理 None。
    """
    devs = list_input_devices()
    if not devs:
        return None

    # ① 用户显式选过 → 优先它（换口/重启后索引会变，所以按"身份"找）
    want = _load_preferred_input()
    if want:
        for d in devs:
            if device_identity(d["name"]) == want:
                return d["index"]

    reals = [d for d in devs if is_real_device(d["name"])]

    # ②/③ 按能力打分（而不是"名字含 usb 的第一个"）
    best = pick_input_device(reals, prefer=prefer)
    if best is not None:
        return best["index"]

    # ④ 退而求其次：系统默认 → 第一个
    try:
        default_in = _sd.default.device[0]
        if default_in is not None and default_in >= 0:
            return default_in
    except Exception:
        pass
    return devs[0]["index"]


# ── 记住用户手动选的麦克风 ────────────────────────────────────────
#: 落盘位置（每个用户一份，0600）。换口/重启后索引会变，
#: 所以存的是**设备身份字符串**（见 `device_identity`），不是索引。
_MIC_PREF_PATH = os.path.join(
    os.path.expanduser("~"), ".config", "car_gui", "mic_device.txt")


def _load_preferred_input():
    """读回用户上次选的麦克风身份（没有则返回 ""）。

    ★ 顺带**自动迁移旧格式**：早期版本会把 GUI 下拉框的 `[0] ` 前缀
      一起存进来（`[0] usb audio:`）。这里读到时就地规范化并回写，
      否则老用户升级后那条偏好会永远匹配不上（静默失效）。
    """
    try:
        with open(_MIC_PREF_PATH, encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return ""
    if not raw:
        return ""
    norm = device_identity(raw)
    if norm and norm != raw:
        # 旧格式 → 规范化后回写（失败也无所谓，不影响本次返回）
        try:
            with open(_MIC_PREF_PATH, "w", encoding="utf-8") as f:
                f.write(norm)
        except Exception:
            pass
    return norm


def save_preferred_input(name_or_index):
    """记住用户选的麦克风。传设备名或 sounddevice 索引。

    返回保存的身份字符串；失败返回 ""。
    """
    ident = ""
    try:
        if isinstance(name_or_index, int):
            ident = device_identity(_sd.query_devices(name_or_index)["name"])
        else:
            ident = device_identity(str(name_or_index))
    except Exception:
        return ""
    if not ident:
        return ""
    try:
        d = os.path.dirname(_MIC_PREF_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(_MIC_PREF_PATH, "w", encoding="utf-8") as f:
            f.write(ident)
        try:
            os.chmod(_MIC_PREF_PATH, 0o600)
        except Exception:
            pass
        return ident
    except Exception:
        return ""


def clear_preferred_input():
    """忘掉用户的选择（回到自动打分）。"""
    try:
        os.unlink(_MIC_PREF_PATH)
        return True
    except Exception:
        return False


def audio_environment_hint():
    """
    当「一个设备都看不到」时，给一句人能看懂的诊断。

    这是本次踩坑的产物：板子桌面上 PulseAudio 会独占声卡，
    此时 PortAudio 可能既连不上 PA、又直连不了 ALSA → 设备数 0。

    ⚠️ 但如果根因是**根本没装 sounddevice**，那就是另一回事了——
    这时设备数也是 0，却跟声卡无关。必须分开报，否则会把用户
    引到「查 PulseAudio」的错路上。所以这里先判依赖。

    返回 (是否正常, 提示文本)。
    """
    # 先排除「依赖没装」——它也会让设备数变 0，但根因完全不同
    dep_need = [m for m, ok in (("numpy", HAS_NUMPY),
                                ("sounddevice", HAS_SOUNDDEVICE)) if not ok]
    if dep_need:
        _, hint = dependency_hint(dep_need)
        return False, hint

    devs = list_input_devices()
    if devs:
        return True, ""

    lines = ["检测不到任何录音设备。可能原因："]
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    pulse = os.environ.get("PULSE_SERVER", "")
    has_pa = bool(xdg) or bool(pulse)
    if has_pa:
        lines.append("  · 当前环境有 PulseAudio（XDG_RUNTIME_DIR=%s），"
                     "但 PortAudio 连不上它" % xdg)
        lines.append("    对策 1：改用 ALSA 直连（清掉 PA 环境变量）：")
        lines.append("        env -u XDG_RUNTIME_DIR -u PULSE_SERVER python3 <脚本>")
        lines.append("    对策 2：确认 PA 在跑：pactl info；"
                     "录音源：pactl list short sources")
    else:
        lines.append("  · 没有任何 XDG_RUNTIME_DIR/PULSE_SERVER 环境变量")
        lines.append("  · 声卡可能被 PulseAudio 独占（板子桌面登录后常见）")

    lines.append("  · 检查硬件：lsusb | grep 08bb（PCM2902）；cat /proc/asound/cards")
    lines.append("  · 检查独占：fuser -v /dev/snd/*")
    return False, "\n".join(lines)


def _resample_to_16k(samples, src_rate):
    """把任意采样率的 float32 单声道数组线性重采样到 16kHz（VAD 用）。"""
    if not HAS_NUMPY or src_rate == ASR_SAMPLE_RATE or len(samples) == 0:
        return samples
    n_out = int(round(len(samples) * ASR_SAMPLE_RATE / float(src_rate)))
    if n_out <= 1:
        return _np.zeros(0, dtype=_np.float32)
    x_old = _np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
    x_new = _np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return _np.interp(x_new, x_old, samples).astype(_np.float32)


def _rms(samples):
    if not HAS_NUMPY or len(samples) == 0:
        return 0.0
    return float(_np.sqrt(_np.mean(_np.square(samples))))


def clean_text(text):
    """
    清理 SenseVoice 输出里可能残留的特殊标记，并去掉首尾空白。

    SenseVoice 原始输出可能带 <|zh|><|NEUTRAL|><|Speech|><|woitn|> 这类标签，
    sherpa-onnx 通常会过滤掉，这里再兜一层，防止它们干扰指令解析。
    """
    if not text:
        return ""
    t = text.strip()
    # 去掉 <|...|> 形式的标签
    while "<|" in t and "|>" in t:
        i = t.find("<|")
        j = t.find("|>", i)
        if j < 0:
            break
        t = t[:i] + t[j + 2:]
    # 去掉常见控制类小标签残留
    for tag in ("<unk>", "</s>", "<s>", "[unk]", "(unk)"):
        t = t.replace(tag, "")
    # 合并连续空白
    t = " ".join(t.split())
    return t.strip()


# ══════════════════════════════════════════════════════════════════════
#  录音器
# ══════════════════════════════════════════════════════════════════════

# 已探测成功的采样率缓存。避免每次都先撞一次 16kHz 失败
# （PCM2902 这类免驱声卡硬件只支持 44100/48000）。
# key 用**设备名**而不是设备号 —— 声卡重插后 index 会变，名字不会。
_RATE_CACHE = {}

#: 跨进程持久化的采样率缓存。
#:
#: 为什么需要：`_RATE_CACHE` 是**进程内**字典，GUI 一重启就清空 →
#: 下次录音又要先撞一次 16kHz（必然抛 `paInvalidSampleRate` + 刷一屏 ALSA 报错）。
#: 而实测 PCM2902 的可用采样率是**固定的 44100**，完全没必要每次重新探测。
#:
#: 路径选在系统临时区（不在仓库目录里，避免污染工作区、也不会被 git 跟踪）。
#: 文件损坏/无权限都只是"退化成进程内缓存"，**绝不能让缓存问题影响录音**。
_RATE_CACHE_FILE = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or "/tmp", "car_gui_audio_rates.json")


def _rate_cache_load():
    """把持久化的采样率缓存读进内存。失败静默忽略。"""
    try:
        if not os.path.isfile(_RATE_CACHE_FILE):
            return
        with open(_RATE_CACHE_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, dict):
            for k, v in obj.items():
                try:
                    _RATE_CACHE[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass


def _rate_cache_store(key, rate):
    """记住某个设备可用的采样率（内存 + 落盘）。失败静默忽略。"""
    try:
        _RATE_CACHE[key] = int(rate)
    except (TypeError, ValueError):
        return
    try:
        d = os.path.dirname(_RATE_CACHE_FILE)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = _RATE_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_RATE_CACHE, f)
        os.replace(tmp, _RATE_CACHE_FILE)     # 原子替换，避免读到半个文件
    except Exception:
        pass


def _dev_cache_key(device):
    """把 sounddevice 的设备号/名转成一个稳定的缓存 key。"""
    if device is None:
        return "__default__"
    if isinstance(device, str):
        return device
    try:
        info = _sd.query_devices(device, "input")
        return str(info.get("name", device))
    except Exception:
        return str(device)


def alsa_capture_free(device):
    """采集设备**此刻是否空闲**（ALSA 说 `closed`）。查不到时返回 True。

    🔴🔴 为什么必须有它（2026-10-11 修用户报的「持续监听+急停一起开就闪退」）：

    用户实测崩溃日志：
        python3: src/hostapi/alsa/pa_linux_alsa.c:2178:
          PaAlsaStream_Initialize: Assertion
          `self->capture.nfds || self->playback.nfds' failed.
        （前面是一长串 AlsaOpen 失败）

    这是 **PortAudio 的 ALSA 后端的断言 abort**，不是 Python 异常 ——
    `try/except` **抓不住，进程直接闪退**。
    触发条件：**反复用失败的参数去 `Pa_OpenStream`**（本板麦克风只有 1 个
    subdevice，被 KWS / 播报占着时必然失败），失败堆到一定次数就 abort。

    ⇒ **在碰 PortAudio 之前，先问 ALSA 设备是不是空闲**：
      · `/proc/asound/cardN/pcmMc/sub0/status` == `closed` → 空闲，可以开；
      · 其它值（RUNNING/PREPARED/SETUP/OPEN）→ **被别人占着，连试都别试**，
        直接返回"设备忙"让上层等/让位。
    这样**一次失败的 AlsaOpen 都不会发生**，从根上消除 abort 风险。

    ⚠️ 拿不到这个文件时**返回 True**（保守放行）—— 不能因为读不到 /proc
       就让麦克风彻底用不了。那种情况行为与加这个检查之前一致。
    """
    try:
        info = _sd.query_devices(device, "input")
        name = str(info.get("name", ""))
    except Exception:
        return True
    m = re.search(r"hw:(\d+),(\d+)", name)
    if not m:
        return True                      # 虚拟设备（pulse/default）没有这个文件
    path = "/proc/asound/card%s/pcm%sc/sub0/status" % (m.group(1), m.group(2))
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception:
        return True
    if not txt:
        return True
    # 形如 "closed\n" 或 "state: RUNNING\n"
    return txt.startswith("closed")


def alsa_capture_rates(device):
    """查 ALSA 层**当前**实际生效/可用的录音采样率。查不到返回 []。

    ⚠️ 实测限制（2026-09-29）：`/proc/asound/cardN/pcmMc/sub0/hw_params`
    **只在录音进行中有内容**，设备空闲时是空的 —— 而"录音前"恰恰是空闲状态。
    所以这个函数**多数时候会返回 []**，只在"设备正被别的进程录制"时能给出真实值。

    那为什么还留着？因为「设备正被占用」正是我们最需要判断的场景之一
    （能拿到对方用的率，就知道该不该试同一个率）。拿不到就返回 []，
    调用方会**退回盲试**的老路 —— 行为与以前一致，不会更差。

    ❌ 不要在这里退回解析 `rate_min`/`rate_max`：那是**线性范围**，
       PCM2902 会给出 8000~48000，但 22050/32000 实际打不开，
       拿它当"支持列表"会引入**新的**失败。
    """
    try:
        info = _sd.query_devices(device, "input")
        name = str(info.get("name", ""))
    except Exception:
        return []
    m = re.search(r"hw:(\d+),(\d+)", name)
    if not m:
        return []
    path = "/proc/asound/card%s/pcm%sc/sub0/hw_params" % (m.group(1), m.group(2))
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return []
    # 形如 "rate: 44100 (44100/1)"
    rates = [int(x) for x in re.findall(r"^\s*rate:\s*(\d+)", txt, re.M)]
    return sorted(set(rates))


# ── 打开录音失败的两类错误（★ 必须分开处理，2026-09-29 新增）──────────
#
#  症状：界面频繁报
#      "打开麦克风失败（设备 0）：Error opening InputStream:
#       Unanticipated host error [PaErrorCode -9999]: 'Input/output error' [ALSA error -5]"
#  并伴随一屏 `paInvalidSampleRate` / `PaAlsaStreamComponent_FinishConfigure`
#  之类的 PortAudio 内部表达式报错（那些是**噪声**，真正的错在最后一句）。
#
#  这两类混在一起时，"换采样率重试"是错的解法：
#    · RATE 类（如 paInvalidSampleRate, -9997）：**这个采样率不被支持** → 换一个试；
#    · BUSY 类（如 Input/output error, -9999, Device or resource busy, -16）：
#      **设备被占 / 还没来得及释放** → 换采样率没用，**只能等一会儿重开**。
_RATE_ERR_MARKERS = (
    "invalid samplerate", "-9997", "invalid sample rate",
    "samplerate", "sample rate", "not supported", "unsupported",
)
_BUSY_ERR_MARKERS = (
    "input/output error", "-9999", "unanticipated host error",
    "device or resource busy", "resource busy", "busy",
    "[alsa error -5]", "alsa error -16", "errno -16", "errno -5",
    "device unavailable", "-9985", "no default input device",
)


def _is_rate_error(err):
    """这个异常是"采样率不被支持"吗？"""
    s = str(err).lower()
    # "-9999" 同时出现在两类串里，但 BUSY 的判断优先（见 _classify_open_error）
    return any(m in s for m in _RATE_ERR_MARKERS)


def _is_busy_error(err):
    """这个异常是"设备被占/没释放好"吗？（重开就好，换采样率没用）"""
    s = str(err).lower()
    return any(m in s for m in _BUSY_ERR_MARKERS)


def _classify_open_error(err):
    """返回 'busy' / 'rate' / 'other'。

    ⚠️ 判定顺序很重要：`-9999 Unanticipated host error` 里**同时**能匹配到
    两类关键词，但它的实质是底层 ALSA 出错（多为设备忙）→ **必须先判 busy**。
    """
    if _is_busy_error(err):
        return "busy"
    if _is_rate_error(err):
        return "rate"
    return "other"


def friendly_audio_error(err, device=None):
    """把 PortAudio/ALSA 的原始报错翻成**人话**，只留最有用的那句。

    原始报错是一大坨 C 表达式（`PaAlsaStreamComponent_FinishConfigure ...`），
    直接显示在界面上既吓人又没用。这里提炼成一句可操作的提示。
    """
    kind = _classify_open_error(err)
    s = str(err)
    if kind == "busy":
        return ("麦克风被占用（可能是「实时监听/语音急停」正在用，"
                "或上一次录音还没完全释放）。请稍等一两秒再试；"
                "若一直失败，先关掉「语音急停」或「开始聆听」。")
    if kind == "rate":
        return ("麦克风不支持这个采样率。本板 USB 声卡通常只支持 "
                "44100/48000 Hz，请在「麦克风」下拉里重选设备后重试。")
    # other：保留原始信息，但截断，避免刷一屏
    one = " ".join(s.split())
    return (one[:200] + "…") if len(one) > 200 else (one or "未知错误")



def _default_input_rate(device=None, fallback=44100):
    """
    安全地取一个输入设备的默认采样率。

    ⚠️ 不要直接 `_sd.query_devices(None, "input")` ——
       在 PulseAudio 环境下 device=None 会报 `Error querying device -1`。
       这里统一兜底到 44100（USB 声卡的常见值）。
    """
    try:
        info = _sd.query_devices(device, "input")
        rate = int(info.get("default_samplerate", 0) or 0)
        return rate if rate > 0 else fallback
    except Exception:
        return fallback


class Recorder(object):
    """
    后台线程录音，累积到内存。

    用法：
        r = Recorder(device=1)
        r.start()               # 开始录
        ...                     # 期间可读 r.level 显示电平
        rate, samples = r.stop()  # 停止并取回音频
    """
    def __init__(self, device=None, prefer_rate=ASR_SAMPLE_RATE, channels=1):
        self.device = device
        self.prefer_rate = prefer_rate
        self.channels = channels
        self.samplerate = 0
        self._stream = None
        self._chunks = []
        self._lock = threading.Lock()
        self._level = 0.0
        self._running = False
        #: 最近一次打开失败/重试的可读说明（界面可用来提示）—— 见 friendly_audio_error
        self._last_open_note = ""

    @property
    def level(self):
        """当前输入电平（RMS，0~1 左右），界面用它画电平条。"""
        return self._level

    @property
    def running(self):
        return self._running

    def total_samples(self):
        with self._lock:
            return sum(len(c) for c in self._chunks)

    @property
    def seconds(self):
        return self.total_samples() / float(self.samplerate or self.prefer_rate)

    def pending(self, cursor):
        """
        取回自 cursor（**块下标**，不是样本数）之后新增的音频。

        返回 (新的 cursor, float32 数组)。用于"边录边判"的自动聆听场景——
        避免每次都把整段录音重新 concatenate 一遍。
        """
        with self._lock:
            chunks = self._chunks
            new = chunks[cursor:]
            total = len(chunks)
        if not new:
            return total, _np.zeros(0, dtype=_np.float32)
        if len(new) == 1:
            return total, new[0]
        return total, _np.concatenate(new)

    def _callback(self, indata, frames, time_info, status):
        if status:
            # 溢出之类的状态，不中断录音
            pass
        try:
            mono = indata[:, 0].copy()
        except Exception:
            return
        with self._lock:
            self._chunks.append(mono)
        self._level = _rms(mono)

    def _open(self, rate):
        return _sd.InputStream(
            device=self.device,
            samplerate=rate,
            channels=self.channels,
            dtype="float32",
            blocksize=0,
            latency="high",              # 优先稳定，不追低延迟
            callback=self._callback,
        )

    def start(self):
        """开始录音。返回 (是否成功, 提示)。"""
        if not HAS_SOUNDDEVICE or not HAS_NUMPY:
            return False, "缺少 sounddevice 或 numpy，请先运行 bash install_deps.sh"
        if self._running:
            return True, "已在录音"

        # ⚠️ device=None 时先自己挑一个。绝不能把 None 直接传给 query_devices()，
        #    那会报 `Error querying device -1`（PulseAudio 环境下尤其常见）。
        if self.device is None:
            self.device = guess_input_device()
        if self.device is None:
            ok, hint = audio_environment_hint()
            return False, hint or "检测不到录音设备（麦克风是否插好？）"

        with self._lock:
            self._chunks = []
        self._level = 0.0

        # ★★ 开麦前先问 ALSA"设备空闲吗"（2026-10-11 修闪退，见 `alsa_capture_free`）：
        #    本板采集只有 1 个 subdevice，被 KWS / 播报占着时 `Pa_OpenStream`
        #    必然失败；而**反复失败的 AlsaOpen 会让 PortAudio 断言 abort
        #    （try/except 抓不住，进程直接闪退）**。
        #    在这里拦住，就不会产生任何一次失败的 AlsaOpen。
        if not alsa_capture_free(self.device):
            self._last_open_note = "ALSA 报告设备被占用"
            return False, ("麦克风被占用（多半是语音急停正在监听）"
                           "，稍后自动重试")

        # 采样率选择策略（2026-09-26 实测优化，2026-09-29 加固）：
        #   PCM2902 这类免驱声卡**硬件只支持 44100/48000**；虽然 ALSA 的 plug 层
        #   也能接受 16000（驱动重采样），但 PortAudio 对 16000 的协商**会失败并
        #   刷一屏 `paInvalidSampleRate`**（实测）。
        #
        #   ★ 所以顺序改为：**设备默认率优先**，`prefer_rate`(16000) 降为兜底。
        #     依据：① `recognize()` 内部本来就会把任意率重采样到 ASR_SAMPLE_RATE，
        #            所以"录到 44100"完全没问题（实测 KWS/ASR 都正常）；
        #          ② PortAudio 的**设备默认率是它最可能一次成功的率**；
        #          ③ 这样冷启动（无缓存）第一次录音就不再报错、不再留噪音。
        #
        #   ★ 2026-09-29 另加：**区分错误类型**（见 _classify_open_error）。
        #     界面上频繁出现的 `-9999 Input/output error [ALSA -5]`
        #     **不是采样率问题，而是设备被占**（本板麦克风只有 1 个 subdevice，
        #     与 KWS 常驻监听硬件级互斥）→ 换采样率治不了，只能等/让位。
        _rate_cache_load()                       # 读跨进程缓存（首次调用时生效）
        key = _dev_cache_key(self.device)
        default_rate = _default_input_rate(self.device)

        candidates = []
        cached = _RATE_CACHE.get(key)
        if cached:
            candidates.append(cached)            # ① 上次成功的（最可能成功）
        if default_rate and default_rate not in candidates:
            candidates.append(default_rate)      # ② 设备默认率
        if self.prefer_rate not in candidates:
            candidates.append(self.prefer_rate)  # ③ 兜底：prefer_rate(16000)
        candidates.append(None)                  # ④ 真正用设备默认（再问一次）

        #: busy 时的退避等待（秒）—— **只做一轮**，见下面注释。
        _BUSY_BACKOFF = 0.5

        # ★ 若 ALSA 已能告诉我们设备正在用/支持的采样率，就据此过滤候选，
        #   避免"明知不支持还去试"（试一次约 3 秒 + 刷一屏报错）。
        #   ⚠️ 设备空闲时拿不到（见 alsa_capture_rates），此时退回盲试 ——
        #      靠下面的 busy 退避 + 进程内/跨进程缓存兜住体验。
        supported = alsa_capture_rates(self.device)
        if supported:
            filtered = [r for r in candidates if r is None or r in supported]
            candidates = filtered or [supported[-1]]
            self._last_open_note = "设备支持 %s Hz" % (supported,)

        # ★★ 为什么"设备忙"时不再逐采样率重试（2026-09-29 实测教训）：
        #   盲试一个打不开的采样率**本身就要约 3 秒**（PortAudio 内部重试），
        #   3 个候选 × 3 次 busy 重试 = 累计 **12.6 秒**才报错，用户等得莫名其妙。
        #   而"设备忙"换采样率**根本治不了** —— 之前 KWS 占着麦克风时，
        #   三种采样率 5 次试验全失败（0/5），一次都没救回来反而白等 12.6s。
        #   现在：busy → **只等一轮**（够等 ALSA 释放），仍失败就立刻如实报"被占用"，
        #        让用户/上层（mic lease）去解决占用问题。
        last_err = None
        busy_waited = False
        for rate in candidates:
            stream = None
            try:
                r = _default_input_rate(self.device) if rate is None else rate
                stream = self._open(r)
                stream.start()
                self._stream = stream
                self.samplerate = r
                self._running = True
                _rate_cache_store(key, r)         # 记住（内存 + 落盘）
                return True, "录音中 @ %d Hz" % r
            except Exception as e:
                last_err = e
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
                kind = _classify_open_error(e)
                if kind == "busy":
                    if busy_waited:
                        break                     # 已等过一轮，别再多试
                    busy_waited = True
                    self._last_open_note = "设备忙，%.1fs 后重试一次" % _BUSY_BACKOFF
                    time.sleep(_BUSY_BACKOFF)
                    # ★ 同一个采样率再试一次（换率治不了"忙"）
                    try:
                        stream2 = self._open(r)
                        stream2.start()
                        self._stream = stream2
                        self.samplerate = r
                        self._running = True
                        _rate_cache_store(key, r)
                        return True, "录音中 @ %d Hz（等待后成功）" % r
                    except Exception as e2:
                        last_err = e2
                        try:
                            if stream2 is not None:
                                stream2.close()
                        except Exception:
                            pass
                        break                     # 还是忙 → 直接如实报错
                # rate / other → 换下一个候选采样率
                continue

        self._last_open_note = friendly_audio_error(last_err, self.device)
        return False, "打开麦克风失败（设备 %s）：%s" % (
            self.device, friendly_audio_error(last_err, self.device))

    def stop(self):
        """停止录音，返回 (采样率, float32 单声道数组)。"""
        self._running = False
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:
            pass
        self._stream = None
        with self._lock:
            chunks = self._chunks
            self._chunks = []
        self._level = 0.0
        if not chunks:
            return self.samplerate or self.prefer_rate, _np.zeros(0, dtype=_np.float32) \
                if HAS_NUMPY else None
        return self.samplerate, _np.concatenate(chunks).astype(_np.float32)


# ══════════════════════════════════════════════════════════════════════
#  端点检测（自动断句）
# ══════════════════════════════════════════════════════════════════════
class EnergyEndpointDetector(object):
    """
    纯 numpy 的能量端点检测（Silero VAD 不可用时的兜底）。

    原理：以帧为单位算 RMS，用开头若干帧估一个噪声底噪，超过底噪数倍即认为是
    说话。连读检测到足够长的静音就判定句子结束。带自适应底噪更新，能适应
    环境底噪缓变。
    """

    def __init__(self, frame_ms=30, start_ratio=3.0, keep_ratio=2.0,
                 noise_alpha=0.98, abs_floor=0.004):
        self.frame_ms = frame_ms
        self.start_ratio = start_ratio
        self.keep_ratio = keep_ratio
        self.noise_alpha = noise_alpha
        self.abs_floor = abs_floor
        self.reset()

    def reset(self):
        self.noise = None
        self.in_speech = False
        self.speech_ms = 0.0
        self.silence_ms = 0.0
        self._lead_ms = 0.0

    def feed(self, rms):
        """喂一帧的 RMS，返回：'idle' / 'speech' / 'end'"""
        if self.noise is None:
            self.noise = max(rms, self.abs_floor)
        # 底噪慢速更新（只在非语音期更新，避免把说话声算进底噪）
        if not self.in_speech:
            self.noise = self.noise * self.noise_alpha + rms * (1 - self.noise_alpha)
        self.noise = max(self.noise, 1e-6)

        if not self.in_speech:
            self._lead_ms += self.frame_ms
            if rms > max(self.noise * self.start_ratio, self.abs_floor * 2):
                self.in_speech = True
                self.speech_ms = self.frame_ms
                self.silence_ms = 0.0
                return "speech"
            return "idle"

        # 已在语音中
        if rms > max(self.noise * self.keep_ratio, self.abs_floor):
            self.speech_ms += self.frame_ms
            self.silence_ms = 0.0
            return "speech"
        self.silence_ms += self.frame_ms
        if self.silence_ms >= AUTO_TAIL_SILENCE * 1000:
            return "end"
        return "speech"


class SileroEndpointDetector(object):
    """
    基于 sherpa-onnx 自带 Silero VAD 的端点检测（更抗噪，优先使用）。

    若构造失败（版本接口变动、缺 silero_vad.onnx 等），由上层回退到
    EnergyEndpointDetector。
    """

    def __init__(self, model_path, sample_rate=ASR_SAMPLE_RATE, min_silence=0.6):
        self.model_path = model_path
        self.out_rate = sample_rate
        self._vad = None
        self._in_rate = None
        self._buf = None
        self._window = 0
        self._started = False
        self._silence_frames = 0
        self.ok = False
        self.error = ""

        try:
            cfg = _sherpa.VadModelConfig()
            cfg.silero_vad.model = model_path
            cfg.silero_vad.min_silence_duration = float(min_silence)
            cfg.silero_vad.min_speech_duration = 0.15
            cfg.sample_rate = int(sample_rate)
            self._window = int(cfg.silero_vad.window_size)
            self._vad = _sherpa.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
            self._buf = _np.zeros(0, dtype=_np.float32)
            self.ok = True
        except Exception as e:
            self.error = str(e)
            self._vad = None

    def reset(self):
        if self.ok:
            try:
                self._vad.reset()
            except Exception:
                pass
            self._buf = _np.zeros(0, dtype=_np.float32)
            self._started = False
            self._silence_frames = 0

    def feed(self, chunk, in_rate):
        """喂一块任意长度的音频，返回 'idle' / 'speech' / 'end'"""
        if not self.ok:
            return "idle"
        if self._in_rate is None:
            self._in_rate = in_rate
        resampled = _resample_to_16k(chunk, in_rate) if in_rate != self.out_rate else chunk
        self._buf = _np.concatenate([self._buf, resampled])
        while len(self._buf) >= self._window:
            self._vad.accept_waveform(self._buf[:self._window])
            self._buf = self._buf[self._window:]

        if self._vad.is_speech_detected():
            self._started = True
            self._silence_frames = 0
            return "speech"
        if self._started:
            self._silence_frames += 1
            # 每帧约 window/16000 秒；累积到 ~0.5 秒静音判定结束
            if self._silence_frames * (self._window / float(self.out_rate)) >= 0.5:
                return "end"
            return "speech"
        return "idle"


# ══════════════════════════════════════════════════════════════════════
#  主引擎
# ══════════════════════════════════════════════════════════════════════
class AsrEngine(object):
    """离线语音识别引擎。所有阻塞方法都返回 (是否成功, 结果或错误说明)。"""

    def __init__(self, log=None, level=None):
        """
        log(text)    : 日志回调（可为 None）
        level(value) : 输入电平回调 0~1（可为 None）
        """
        self._log_cb = log
        self._level_cb = level

        self._rec = None                 # sherpa_onnx.OfflineRecognizer
        self._model_path = None
        self._tokens_path = None
        self._num_threads = DEFAULT_THREADS
        self._language = DEFAULT_LANGUAGE
        self._use_itn = True
        self._model_dir = ""

        self._recorder = None
        self._ptt_active = False
        self._ptt_start_ts = 0.0
        self._last_text = ""
        self._last_cost = 0.0
        self._vad_mode = "none"          # silero / energy / none
        self._silero_path = None

    # ── 日志 ────────────────────────────────────────────────────────
    def _log(self, text):
        if self._log_cb:
            try:
                self._log_cb(text)
                return
            except Exception:
                pass
        print(text, flush=True)

    # ── 状态 ────────────────────────────────────────────────────────
    @property
    def ready(self):
        return self._rec is not None

    @property
    def recording(self):
        return bool(self._recorder and self._recorder.running)

    @property
    def level(self):
        return self._recorder.level if self._recorder else 0.0

    @property
    def last_text(self):
        return self._last_text

    @property
    def model_dir(self):
        return self._model_dir

    @property
    def vad_mode(self):
        return self._vad_mode

    def describe(self):
        if not self.ready:
            return "未加载"
        return "%s · %s · %d 线程 · 断句:%s" % (
            os.path.basename(self._model_dir or "?"),
            self._language, self._num_threads, self._vad_mode)

    # ── 加载模型 ────────────────────────────────────────────────────
    def load(self, model_dir=None, num_threads=DEFAULT_THREADS,
             language=DEFAULT_LANGUAGE, use_itn=True):
        """加载 SenseVoice 模型。返回 (是否成功, 提示信息)。"""
        if not _HAS_SHERPA:
            msg = ("缺少 sherpa-onnx（%s）。请运行：bash install_deps.sh"
                   % (_SHERPA_ERROR or "未安装"))
            self._log(msg)
            return False, msg
        if not HAS_NUMPY:
            msg = "缺少 numpy，请运行：bash install_deps.sh"
            self._log(msg)
            return False, msg

        model_dir = model_dir or guess_model_dir()
        model, tokens = find_model_files(model_dir)
        if not model:
            msg = ("在 %s 里找不到模型文件（应为 model.int8.onnx）。\n"
                   "请先运行 bash download_asr_model.sh 下载。" % model_dir)
            self._log(msg)
            return False, msg
        if not tokens:
            msg = "在 %s 里找不到 tokens.txt" % model_dir
            self._log(msg)
            return False, msg

        t0 = time.time()
        self._log("正在加载模型 %s …（228MB，首次约需 10–30 秒）" % os.path.basename(model))
        try:
            rec = _sherpa.OfflineRecognizer.from_sense_voice(
                model=model,
                tokens=tokens,
                num_threads=int(num_threads),
                use_itn=bool(use_itn),
                language=language or DEFAULT_LANGUAGE,
                debug=False,
                provider="cpu",
            )
        except TypeError:
            # 老版本可能不接受 language / provider 参数，逐级降级重试
            try:
                rec = _sherpa.OfflineRecognizer.from_sense_voice(
                    model=model, tokens=tokens, num_threads=int(num_threads),
                    use_itn=bool(use_itn), debug=False)
                self._log("提示：当前 sherpa-onnx 版本不支持 language/provider 参数，已降级")
            except Exception as e:
                msg = "加载模型失败：%s" % e
                self._log(msg)
                return False, msg
        except Exception as e:
            msg = "加载模型失败：%s" % e
            self._log(msg)
            return False, msg

        self._rec = rec
        self._model_path = model
        self._tokens_path = tokens
        self._model_dir = model_dir
        self._num_threads = int(num_threads)
        self._language = language or DEFAULT_LANGUAGE
        self._use_itn = bool(use_itn)

        # 顺便探测 Silero VAD 是否可用，决定断句方式
        self._silero_path = os.path.join(model_dir, SILERO_VAD_FILE)
        if not os.path.isfile(self._silero_path):
            self._silero_path = None
        self._vad_mode = "silero" if self._silero_path else "energy"

        self._log("模型加载完成，用时 %.1f 秒；断句方式：%s"
                  % (time.time() - t0, self._vad_mode))
        return True, self.describe()

    def _make_detector(self):
        """构造一个端点检测器，优先 Silero。"""
        if self._silero_path:
            det = SileroEndpointDetector(self._silero_path)
            if det.ok:
                self._vad_mode = "silero"
                return det
            self._log("Silero VAD 不可用（%s），改用能量断句" % det.error)
            self._vad_mode = "energy"
        return EnergyEndpointDetector()

    # ── 识别 ────────────────────────────────────────────────────────
    def recognize(self, sample_rate, samples):
        """对一段音频做识别，返回 (是否成功, 文本或错误)。"""
        if not self.ready:
            return False, "模型未加载"
        if samples is None or len(samples) == 0:
            return False, "没有录到音频"
        try:
            stream = self._rec.create_stream()
            stream.accept_waveform(int(sample_rate), samples)
            self._rec.decode_stream(stream)
            raw = stream.result.text
        except Exception as e:
            return False, "识别失败：%s" % e

        text = clean_text(raw)
        self._last_text = text
        if not text:
            return False, "没听清（可以靠近麦克风、说慢一点再试）"
        return True, text

    def recognize_file(self, path):
        """识别一个 wav 文件（仅支持 16bit PCM wav），便于离线验证。"""
        try:
            import wave
            with wave.open(path, "rb") as w:
                rate = w.getframerate()
                ch = w.getnchannels()
                width = w.getsampwidth()
                frames = w.readframes(w.getnframes())
        except Exception as e:
            return False, "读取 wav 失败：%s" % e

        if width != 2:
            return False, "只支持 16bit PCM wav（当前 %d bit）" % (width * 8)
        try:
            data = _np.frombuffer(frames, dtype=_np.int16).astype(_np.float32) / 32768.0
        except Exception as e:
            return False, "解析 wav 失败：%s" % e
        if ch > 1:
            data = data.reshape(-1, ch)[:, 0]
        return self.recognize(rate, data)

    # ── 声卡未到位时的替代验证 ───────────────────────────────────────
    def synthesize_test_wav(self, out_path=None, seconds=2.5, src="zh"):
        """
        从模型自带的官方 test_wavs 里裁一段，生成"模拟录音"wav。

        用途：**在 USB 声卡到货前，先把「录音→识别」这条链路的接口契约验证掉。**
        虽然数据不是真麦克风采的，但它是标准 16bit PCM 单声道 wav，
        喂给 Recorder 下游的同一套代码路径（recognize），能提前排掉
        采样率、位深、声道、重采样、静音裁剪等一整类问题。

        返回 (是否成功, 文件路径或错误说明)。
        """
        if not HAS_NUMPY:
            return False, "缺少 numpy"

        md = self._model_dir or guess_model_dir()
        src_path = os.path.join(md, "test_wavs", "%s.wav" % src)
        if not os.path.isfile(src_path):
            return False, "找不到源音频：%s" % src_path

        try:
            import wave
            with wave.open(src_path, "rb") as w:
                rate = w.getframerate()
                ch = w.getnchannels()
                width = w.getsampwidth()
                n_frames = w.getnframes()
                # 跳过开头 0.3 秒，避免切入静音段
                w.setpos(min(int(rate * 0.3), max(n_frames - 1, 0)))
                want = min(int(rate * seconds), n_frames)
                frames = w.readframes(want)
        except Exception as e:
            return False, "读取源音频失败：%s" % e

        if width != 2:
            return False, "源音频不是 16bit（%d bit）" % (width * 8)
        if len(frames) < rate:      # 不足 1 秒就没意义了
            return False, "裁出的音频太短（%d 字节）" % len(frames)

        out_path = out_path or os.path.join(
            md, "test_wavs", "synthetic_ptt_%s.wav" % src)
        try:
            with wave.open(out_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                w.writeframes(frames)
        except Exception as e:
            return False, "写出 wav 失败：%s" % e

        data = _np.frombuffer(frames, dtype=_np.int16)
        if ch > 1:
            data = data.reshape(-1, ch)[:, 0].copy()
        src_rate = rate
        if src_rate != ASR_SAMPLE_RATE:
            data = _resample_to_16k(data.astype(_np.float32) / 32768.0, src_rate)
        else:
            data = data.astype(_np.float32) / 32768.0

        self._synthetic = {
            "path": out_path,
            "rate": ASR_SAMPLE_RATE,
            "seconds": round(len(data) / float(ASR_SAMPLE_RATE), 2),
        }
        return True, out_path

    @property
    def synthetic(self):
        """最近一次 synthesize_test_wav() 的产物信息（无则 None）。"""
        return getattr(self, "_synthetic", None)

    # ── 按住说话（Push-to-Talk）────────────────────────────────────
    def ptt_start(self, device=None):
        """开始按住说话。返回 (是否成功, 提示)。"""
        if not self.ready:
            return False, "模型未加载"
        if self.recording:
            return True, "已在录音"
        self._recorder = Recorder(device=device)
        ok, msg = self._recorder.start()
        if not ok:
            self._recorder = None
            return False, msg
        self._ptt_active = True
        self._ptt_start_ts = time.time()
        return True, msg

    def ptt_stop(self):
        """结束按住说话并识别。返回 (是否成功, 文本或提示)。"""
        if not self._recorder:
            return False, "当前没有在录音"
        rate, samples = self._recorder.stop()
        self._recorder = None
        self._ptt_active = False

        dur = len(samples) / float(rate or ASR_SAMPLE_RATE)
        if dur < PTT_MIN_SECONDS:
            return False, "说话时间太短（%.2fs）" % dur

        t0 = time.time()
        ok, res = self.recognize(rate, samples)
        self._last_cost = time.time() - t0
        if ok:
            self._log("识别：%s  （音频 %.2fs，耗时 %.2fs）" % (res, dur, self._last_cost))
        else:
            self._log("识别失败：%s" % res)
        return ok, res

    # ── 自动聆听（VAD 断句）────────────────────────────────────────
    def listen_auto(self, device=None, timeout=AUTO_TIMEOUT,
                    max_seconds=AUTO_MAX_SECONDS, level_cb=None):
        """
        自动聆听一句话（阻塞）。检测到静音后自动结束并识别。

        返回 (是否成功, 文本或提示)。应在后台线程里调用。
        """
        if not self.ready:
            return False, "模型未加载"

        rec = Recorder(device=device)
        ok, msg = rec.start()
        if not ok:
            return False, msg

        det = self._make_detector()
        self._vad_mode = "silero" if isinstance(det, SileroEndpointDetector) else "energy"
        self._recorder = rec

        # 块大小取 0.1 秒；carry 用来缓存不足一块的零头
        block = max(int(rec.samplerate * 0.1), 160)
        cursor = 0
        carry = _np.zeros(0, dtype=_np.float32)
        t_start = time.time()
        speech_started = False
        lead = 0.0
        #: ★ 已喂给检测器的样本数 / 首次判为"语音"时的位置
        #:   用来**裁掉前导静音**（见下面 finally 之后的裁剪）——
        #:   自动聆听会把"等你开口"的那几秒静音也录进去，
        #:   而识别耗时与音频长度成正比（实测 1.1s 音频 0.25s、
        #:   4.7s 音频 0.81s）→ 裁掉就快。
        fed = 0
        speech_at = None

        try:
            while True:
                time.sleep(0.02)
                if level_cb:
                    try:
                        level_cb(rec.level)
                    except Exception:
                        pass

                cursor, chunk = rec.pending(cursor)
                if len(chunk):
                    carry = chunk if len(carry) == 0 else _np.concatenate([carry, chunk])

                if len(carry) < block:
                    if not speech_started and time.time() - t_start > timeout:
                        return False, "没听到声音（已等 %.0f 秒）" % timeout
                    if time.time() - t_start > max_seconds + timeout:
                        return False, "等待超时"
                    continue

                feed = carry[:block]
                carry = carry[block:]

                if isinstance(det, SileroEndpointDetector):
                    state = det.feed(feed, rec.samplerate)
                else:
                    state = det.feed(_rms(feed))
                fed += len(feed)

                if state == "speech":
                    if not speech_started:
                        speech_at = fed          # ★ 记下语音起点（样本数）
                    speech_started = True
                    lead = 0.0
                elif state == "idle":
                    lead += 0.1
                    if lead > timeout:
                        return False, "没听到声音（已等 %.0f 秒）" % timeout
                elif state == "end":
                    break

                if speech_started and time.time() - t_start > max_seconds:
                    self._log("达到单句最长 %.0f 秒，强制结束" % max_seconds)
                    break
        finally:
            rate, samples = rec.stop()
            self._recorder = None

        dur = len(samples) / float(rate or ASR_SAMPLE_RATE)
        if not speech_started or dur < AUTO_MIN_SPEECH:
            return False, "没检测到有效语音（%.2fs）" % dur

        # ★★ 裁掉前导/尾随静音再送去识别（2026-10-11 提速）：
        #    识别耗时与音频长度成正比（实测 1.1s→0.25s、4.7s→0.81s），
        #    而自动聆听录制里"等你开口"的静音往往占一大半
        #    （实测出现过 音频 4.73s / 6.33s）。
        #    裁剪只动**明显多余的**静音，保留安全边距，不会切到人声。
        raw_dur = dur
        try:
            rate_i = int(rate or ASR_SAMPLE_RATE)
            cut_from = 0
            if speech_at is not None:
                # 语音起点前留 0.25s 边距（检测器本身有滞后）
                cut_from = max(0, int(speech_at) - int(rate_i * 0.25))
                # 头部静音不足 0.3s 就不裁了，免得动到正常前导
                if cut_from < int(rate_i * 0.3):
                    cut_from = 0
            if cut_from > 0 and cut_from < len(samples):
                samples = samples[cut_from:]
            # 尾随静音：找到最后一个"有声"样本，后面留 0.12s
            if len(samples) > 0:
                pk = float(_np.max(_np.abs(samples)))
                thr = max(0.002, pk * 0.05)
                idx = _np.nonzero(_np.abs(samples) > thr)[0]
                if len(idx):
                    keep_to = min(len(samples),
                                  int(idx[-1]) + int(rate_i * 0.12))
                    if keep_to < len(samples):
                        samples = samples[:keep_to]
            dur = len(samples) / float(rate_i)
            if dur < AUTO_MIN_SPEECH:                 # 裁太狠就退回原始
                dur = raw_dur
        except Exception:
            pass

        t0 = time.time()
        ok2, res = self.recognize(rate, samples)
        self._last_cost = time.time() - t0
        if ok2:
            self._log("识别：%s （音频 %.2fs→%.2fs，耗时 %.2fs）" % (
                res, raw_dur, dur, self._last_cost))
        else:
            self._log("识别失败：%s" % res)
        return ok2, res

    def cancel_recording(self):
        """放弃当前录音。"""
        if self._recorder:
            try:
                self._recorder.stop()
            except Exception:
                pass
            self._recorder = None
        self._ptt_active = False


# ══════════════════════════════════════════════════════════════════════
#  命令行自测
# ══════════════════════════════════════════════════════════════════════
def _main():
    import argparse

    ap = argparse.ArgumentParser(description="语音识别引擎自测")
    ap.add_argument("--status", action="store_true", help="打印依赖状态")
    ap.add_argument("--list-devices", action="store_true", help="列出录音设备")
    ap.add_argument("--model-dir", default=None, help="模型目录")
    ap.add_argument("--file", default=None, help="识别一个 16bit PCM wav 文件")
    ap.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    ap.add_argument("--lang", default=DEFAULT_LANGUAGE)
    ap.add_argument("--synthetic-selftest", action="store_true",
                    help="无声卡自测：从官方 test_wavs 裁一段，走完整识别链路")
    ap.add_argument("--syn-seconds", type=float, default=2.5,
                    help="自测音频时长（秒），默认 2.5")
    args = ap.parse_args()

    if args.status or (not args.file and not args.list_devices):
        st = dependency_status()
        print("=" * 56)
        print(" 语音识别依赖状态")
        print("=" * 56)
        for k in ("numpy", "sounddevice", "sherpa_onnx"):
            print("  %-14s %s" % (k, "OK" if st[k] else "缺失"))
        for k, v in st["versions"].items():
            print("      %-12s %s" % (k, v))
        if st["portaudio"]:
            print("      %-12s %s" % ("portaudio", st["portaudio"]))
        if st["sherpa_error"]:
            print("      sherpa 错误：%s" % st["sherpa_error"])
        md = args.model_dir or guess_model_dir()
        model, tokens = find_model_files(md)
        print("\n  模型目录：%s" % md)
        print("  模型文件：%s" % (model or "未找到"))
        print("  tokens  ：%s" % (tokens or "未找到"))
        vad = os.path.join(md, SILERO_VAD_FILE)
        print("  VAD 模型：%s" % (vad if os.path.isfile(vad) else "未找到（将用能量断句）"))

        # 声卡诊断：把"能做的"和"不能做的"一次说清
        devs = list_input_devices()
        print("\n  录音设备：%s" % ("%d 个" % len(devs) if devs else "**无**"))
        if not devs:
            print("    ⚠️  这是**板子没有音频硬件**导致的（不是装漏了）：")
            print("       内核未编译 I2S/ASoC → 板载 MIC 与 HDMI 音频都不可用。")
            print("       CONFIG_SND_USB_AUDIO=y 已内置 → **插 USB 声卡即用、免驱动**。")
            print("    ✅ 在声卡到货前，可先跑离线自测：")
            print("       python3 asr_engine.py --synthetic-selftest")

    if args.list_devices:
        print("\n录音设备：")
        devs = list_input_devices()
        if not devs:
            print("  （没有可用录音设备）")
        for d in devs:
            print("  [%d] %-45s %6d Hz  %d ch" % (d["index"], d["name"][:45],
                                                  d["samplerate"], d["channels"]))
        print("\n建议使用设备号：%s" % guess_input_device())

    if args.synthetic_selftest:
        eng = AsrEngine()
        ok, msg = eng.load(args.model_dir, args.threads, args.lang)
        if not ok:
            print("加载失败：%s" % msg)
            return 1
        ok, path = eng.synthesize_test_wav(seconds=args.syn_seconds)
        if not ok:
            print("生成自测音频失败：%s" % path)
            return 1
        info = eng.synthetic or {}
        print("自测音频：%s（%.2f 秒 @ %d Hz）"
              % (path, info.get("seconds", 0), info.get("rate", 0)))
        ok, res = eng.recognize_file(path)
        if ok:
            print("识别结果：%s" % res)
            print("\n✅ 「录音→识别」链路可用（用合成音频验证，未经过真实声卡）")
        else:
            print("识别失败：%s" % res)
        return 0 if ok else 1

    if args.file:
        eng = AsrEngine()
        ok, msg = eng.load(args.model_dir, args.threads, args.lang)
        if not ok:
            print("加载失败：%s" % msg)
            return 1
        ok, res = eng.recognize_file(args.file)
        print("识别结果：%s" % res)
        return 0 if ok else 1

    return 0


if __name__ == "__main__":
    sys.exit(_main())
