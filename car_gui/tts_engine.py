#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tts_engine.py —— 中文语音合成（sherpa-onnx VITS）

状态（2026-09-27 逐条复核）
--------------------------
**引擎侧完全就绪**：加载 3.7 秒、187 个说话人、16000 Hz、纯 CPU 离线推理。
**播放侧硬件缺位**，而且已确认是硬件层面的事实，不是配置问题：

  ① **板载没有音频硬件** —— 内核没编译 `CONFIG_SND_DESIGNWARE_I2S` /
     `CONFIG_SND_SIMPLE_CARD` / `CONFIG_SND_AUDIO_GRAPH_CARD`；
     `SND_SOC` 下除 I2C/SPI 粘合层外**没有任何具体 codec 驱动**。
     → HDMI 音频同样不可用（没有 codec 驱动，HDMI 出不了声）。

  ② **已插的 TI PCM2902（`08bb:2902`）是纯采集设备** —— `lsusb -v` 显示它的
     AudioStreaming 接口只有一个端点 `0x82 EP 2 IN`，**没有任何 OUT 端点**；
     `/dev/snd/` 里只有 `pcmC0D0c`，没有 `pcmC0D0p`。
     → 那个 3.5mm 口是 **MIC IN**，插耳机不会响。这是硬件事实，改不了。

  ③ **蓝牙也走不通** —— 内核 `# CONFIG_BT is not set`，`drivers/bluetooth/`
     目录不存在、`btusb` 模块也没有。→ AX210 的蓝牙在这套内核上无法启用，
     A2DP 音箱这条"无线"的路直接封死。

  ✅ **唯一的解法**：插一个 **USB 音箱** 或 **USB 声卡（带输出）**。
     内核内置 `CONFIG_SND_USB_AUDIO=y`，符合 UAC 标准的设备**免驱即插即用**，
     插上 `aplay -l` 立刻就有设备，本文件不用改任何代码。
  ✅ **硬件到位之前**：用 `tts_serve.py` 把声音送到 PC/手机浏览器播放 ——
     推理仍在板子上跑，只是"喇叭"换成了浏览器。

模型
    vits-zh-hf-fanchen-C   （113MB，16000Hz，187 个说话人）
    位于 ~/models/tts/vits-zh-hf-fanchen-C/

用法
    # 环境诊断（快，不加载模型）
    bash run_py.sh tts_engine.py --check

    # 合成一句话存成 wav
    bash run_py.sh tts_engine.py --text "已急停" --out /tmp/a.wav

    # 指定说话人与语速
    bash run_py.sh tts_engine.py --text "小车已停止" --speaker 3 --speed 1.1

    # 列出说话人 / 输出设备
    bash run_py.sh tts_engine.py --list-speakers
    bash run_py.sh tts_engine.py --list-devices

    # 合成并播放（🔴 需先接 USB 音箱）
    bash run_py.sh tts_engine.py --text "已急停" --play
    bash run_py.sh tts_engine.py --text "已急停" --play --device plughw:1,0

⚠️ 踩坑记录
    · `vits-icefall-zh-aishell3` 不能用 —— tokens.txt 第一行是 `sil` 而非
      `<sil>`，报 `Error when reading tokens at Line sil 0. size: 3`。
      换 `vits-zh-hf-fanchen-C` 即可（别去改 tokens.txt，改了还有别的格式问题）。
    · 本板无音频输出，`--play` 必然失败；用 `--out` 生成文件，或起 `tts_serve.py`。
    · 合成约 1~4 秒/短句（2 线程 CPU），**不能用于实时对话**，只适合播报。
"""

import fnmatch
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from queue import Queue as _Queue, Empty as _Empty

try:
    import numpy as np
except Exception:
    np = None

_HAS_SHERPA = False
_SH_ERROR = ""
try:
    import sherpa_onnx as _sherpa
    _HAS_SHERPA = True
except Exception as e:
    _sherpa = None
    _SH_ERROR = str(e)


DEFAULT_TTS_DIRS = (
    os.path.expanduser("~/models/tts/vits-zh-hf-fanchen-C"),
    "/data/models/tts/vits-zh-hf-fanchen-C",
    "/home/HwHiAiUser/models/tts/vits-zh-hf-fanchen-C",
)

#: 扫描这些目录，自动发现里面所有可用的 TTS 模型
TTS_ROOT_CANDIDATES = (
    os.path.expanduser("~/models/tts"),
    "/data/models/tts",
    "/home/HwHiAiUser/models/tts",
)

#: **音质优先级**（越靠前，越会被自动选中）。
#: 依据见《13.语音合成TTS接入操作文档.md》§2.3 的实测对比。
MODEL_PREFERENCE = ("kokoro", "melo", "matcha", "vits-zh-hf-fanchen", "vits")

#: 多说话人模型的**首选说话人**。
#: ⚠️ 这些模型里混着多国语言音色，**编号 0 往往是英语音色** ——
#: 拿它念中文会带很重的英文口音。所以不能默认用 0，必须按模型指定。
#: 取值依据是**实测**（见 §2.5），不是从音色名猜的。
#:
#: `47` 已由用户**定稿**（2026-09-27 确认）。改这个值 = 换掉小车的嗓音。
PREFERRED_SPEAKER = {
    "kokoro-multi-lang": 47,
}

#: 已实测能正常读中文的说话人编号区间（左闭右闭）。
#: 供 --list-speakers 提示用户"别乱选"。
ZH_SPEAKER_RANGE = {
    "kokoro-multi-lang": (45, 52),
}


def _model_key(tts_dir=None):
    return os.path.basename((tts_dir or guess_tts_dir()).rstrip("/")).lower()


def default_speaker(tts_dir=None):
    """
    这个模型该用哪个说话人当默认。

    单说话人模型返回 0；多语言模型返回实测好用的那个（见 PREFERRED_SPEAKER）。
    """
    key = _model_key(tts_dir)
    for k, spk in PREFERRED_SPEAKER.items():
        if k in key:
            return spk
    return 0


def zh_speaker_range(tts_dir=None):
    """该模型里能正常读中文的编号区间，没有记录返回 None。"""
    key = _model_key(tts_dir)
    for k, rng in ZH_SPEAKER_RANGE.items():
        if k in key:
            return rng
    return None

# 模型文件名候选
MODEL_CANDIDATES = ("vits-zh-hf-fanchen-C.onnx", "model.onnx")

# ══════════════════════════════════════════════════════════════════════
#  输出设备探测（本板的核心痛点，见文件头）
# ══════════════════════════════════════════════════════════════════════

#: 没有任何输出设备时给出的解释。**写清楚"为什么"，避免下次又去试蓝牙/HDMI。**
NO_OUTPUT_HINT = """\
本机当前**没有任何音频输出设备**，且已确认是硬件层面的限制（三次复核）：

  ① 板载无音频硬件
     内核未编译 SND_DESIGNWARE_I2S / SND_SIMPLE_CARD / SND_AUDIO_GRAPH_CARD，
     SND_SOC 下也没有任何具体 codec 驱动 → 板载 I2S 与 HDMI 音频都出不了声。

  ② 已插的 TI PCM2902（08bb:2902）是【纯采集】
     USB 描述符里 AudioStreaming 只有一个端点 0x82（EP 2 IN），没有 OUT 端点；
     /dev/snd/ 里只有 pcmC0D0c，没有 pcmC0D0p。
     那个 3.5mm 口是 MIC IN，插耳机不会响。

  ③ 蓝牙也救不了
     内核 `# CONFIG_BT is not set`，drivers/bluetooth/ 不存在，无 btusb 模块。

✅ 解法：插一个 USB 音箱 / USB 声卡（带输出）。
   内核已内置 CONFIG_SND_USB_AUDIO=y，免驱即插即用，本文件无需改动。
   插好后用 `aplay -l` 应能看到新卡片，再跑 `--play` 即可。

✅ 不想加硬件：跑 `bash run_py.sh tts_serve.py`
   把声音经网络送到 PC / 手机浏览器播放（推理仍在板子上跑）。\
"""


def _card_names():
    """
    从 `/proc/asound/cards` 读"卡号 → 卡 ID"映射，例如 `{1: "SPEAKER"}`。

    格式形如：
         1 [SPEAKER        ]: USB-Audio - SPEAKER
                              Generic SPEAKER at usb-xhci-hcd...
    （第二行是续行，没有前导数字，正则自然跳过。）
    """
    out = {}
    p = "/proc/asound/cards"
    if not os.path.isfile(p):
        return out
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"\s*(\d+)\s*\[(.+?)\]\s*:\s*(.*)", line)
                if m:
                    out[int(m.group(1))] = m.group(2).strip()
    except Exception:
        pass
    return out


def list_output_devices():
    """
    列出 ALSA 里的播放设备。

    返回 [{"id": "plughw:1,0", "card": 1, "device": 0, "name": "SPEAKER"}]
    `id` 可以直接喂给 `aplay -D <id>`。

    数据源用 `/proc/asound/pcm`（内核给的稳定格式），而不是解析 `aplay -l`
    （给人看的、还带本地化风险）；卡名另从 `/proc/asound/cards` 取，
    这样能显示 "SPEAKER" 这种一眼认得出的名字。
    """
    devs = []
    pcm = "/proc/asound/pcm"
    if not os.path.isfile(pcm):
        return devs
    names = _card_names()
    try:
        with open(pcm, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "playback" not in line:
                    continue
                head, _, rest = line.partition(":")     # "01-00: USB Audio : ..."
                card, _, dev = head.strip().partition("-")
                if not (card.isdigit() and dev.isdigit()):
                    continue
                c, d = int(card), int(dev)
                devs.append({
                    # ⚠️ 转成 int 再拼：/proc/asound/pcm 里是 "01-00"，
                    #    直接拼会得到 plughw:01,00（能跑但难看、日志容易看错）
                    "id": "plughw:%d,%d" % (c, d),
                    "card": c,
                    "device": d,
                    "name": (names.get(c)
                             or rest.strip().split(":")[0].strip()
                             or "音频输出"),
                })
    except Exception:
        pass
    return devs


def list_pa_sinks():
    """
    列出 PulseAudio / PipeWire 的播放 sink。

    注意：`pactl` 需要正确的 XDG_RUNTIME_DIR（桌面会话用户）。
    通过 SSH 以 root 跑时通常取不到，此时返回空列表 —— 这是正常的，
    不代表"没有输出设备"。
    """
    if not shutil.which("pactl"):
        return []
    try:
        r = subprocess.run(["pactl", "list", "short", "sinks"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=4)
        if r.returncode != 0:
            return []
        out = r.stdout.decode("utf-8", "replace")
    except Exception:
        return []
    sinks = []
    for line in out.splitlines():
        parts = [p for p in line.split("\t") if p]
        if len(parts) >= 2:
            sinks.append({"id": parts[1], "name": parts[1]})
    return sinks


def output_probe():
    """
    一次性探明输出侧家底。返回 dict：

        ok       是否**存在**任何输出设备
        alsa     ALSA 播放设备列表
        sinks    PulseAudio sink 列表
        players  系统里可用的播放器命令
    """
    alsa = list_output_devices()
    sinks = list_pa_sinks()
    players = [p for p in ("paplay", "aplay", "play", "ffplay")
               if shutil.which(p)]
    return {"ok": bool(alsa) or bool(sinks), "alsa": alsa, "sinks": sinks,
            "players": players}


# ══════════════════════════════════════════════════════════════════════
#  音频工具：wav 编码 / 分句
# ══════════════════════════════════════════════════════════════════════

def pcm_to_wav_bytes(rate, data):
    """
    把 float32 单声道转成**内存里的** 16bit wav 字节。

    `tts_serve.py` 靠它把音频直接吐给浏览器，不用落盘。
    """
    if np is None or data is None or len(data) == 0:
        return None
    import io
    import wave
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(rate))
        f.writeframes(pcm.tobytes())
    return buf.getvalue()


#: 句末标点 —— 同时决定「在哪里断句」。
#:
#: ⚠️ **逗号也必须算**（2026-09-30 修）：
#: 只有句末标点时，`我是你的智能小车，还没连上你呢`（15 字，含逗号）
#: 会被当成**一整句**不切 → NPU 后端实测需要 **284 帧 > 250 帧上限**
#: → 末尾**静默丢字**（用户听到的就是"最后两个字没读出来"）。
#: 逗号本就是天然的换气点，在这里断句听感没问题，而且能救回被截掉的尾巴。
_SENT_PUNCT = "。！？!?；;，,、\n\r"

#: **句末**标点 —— 遇到就一定要断句（语义上句子结束了）
_TERM_PUNCT = "。！？!?；;\n\r"
#: **句中**标点（逗号、顿号）—— 只有当前片段**已经太长**时才拿来当切点。
#:
#: 🔴 为什么要把逗号单独拎出来（2026-10-09 实测）：
#:   旧实现对 `_SENT_PUNCT` 里**每个字符**都立即切，逗号也在里面
#:   → `"嗨，我是你的小车"`（8 字）被切成 2 段 `["嗨，", "我是你的小车"]`，
#:     **白白多一次 ~1.1s 的 NPU 合成 → 多一次明显的段间停顿**。
#:   逗号本来就是**自然换气点**，短句根本不用切；只有长句子才需要
#:   用逗号找一个自然的断开位置。
_SOFT_PUNCT = "，,、"


def split_sentences(text, max_len=36):
    """
    把长文本切成适合播报的短句列表。

    为什么需要：本板 RTF ≈ 3.2（合成 3.2 秒才产出 1 秒语音）。
    整段一次合成会让"第一声"等很久；切句后可以**合成一句、播一句**，
    首句出声快得多，也不会因为一句话太长而卡住整个播报。

    切分依据（2026-10-09 修订）：
      · **句末标点**（。！？；换行）→ 必切；
      · **逗号/顿号** → 只有当"当前片段已超过 `max_len`"时才作为切点，
        否则**保留在句子里**；
      · 片段达到 `max_len` 且附近没有逗号 → 直接切。

    ⚠️ `max_len` 应取 **>14** 的值（默认取 24）：
      真正的"防截断"由 `npu_tts_engine._split_for_frames()` 按**帧数**兜底，
      这里只要避免"把短句切碎"即可。设太小会把 8~14 字的短句也切成段，
      徒增停顿。
    """
    text = (text or "").strip()
    if not text:
        return []
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in _TERM_PUNCT:
            s = buf.strip()
            if s:
                parts.append(s)
                buf = ""
            continue
        if len(buf) >= max_len:
            # 片段够长了 → 尽量在**最近的软标点**处断开（听感更自然）
            s = buf.strip()
            cut = -1
            for i in range(len(s) - 1, max(0, len(s) - 12) - 1, -1):
                if s[i] in _SOFT_PUNCT:
                    cut = i
                    break
            if cut > 0:
                parts.append(s[:cut + 1].strip())
                buf = s[cut + 1:]
            else:
                parts.append(s)
                buf = ""
    s = buf.strip()
    if s:
        parts.append(s)
    return parts or [text]


# ══════════════════════════════════════════════════════════════════════
#  模型定位
# ══════════════════════════════════════════════════════════════════════

def list_tts_dirs():
    """
    扫出所有装好的 TTS 模型目录（含 tokens.txt + onnx 才算）。

    ⚠️ **必须按 realpath 去重**：`~/models/tts` 和 `/home/HwHiAiUser/models/tts`
    在 HwHiAiUser 下是同一个目录，不去重会把每个模型列两遍。
    """
    out, seen = [], set()
    for root in TTS_ROOT_CANDIDATES:
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except Exception:
            continue
        for name in names:
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            real = os.path.realpath(d)
            if real in seen:
                continue
            info = inspect_tts_dir(d)
            if info.get("tokens") and (info.get("model") or info.get("acoustic")):
                seen.add(real)
                out.append(d)
    return out


def guess_tts_dir():
    """
    自动挑模型目录：**有多个时按 MODEL_PREFERENCE 取音质最好的那个**。

    这样装上新模型（比如 kokoro）之后，所有脚本不用改参数就自动用上。
    """
    dirs = list_tts_dirs()
    if dirs:
        def score(d):
            b = os.path.basename(d).lower()
            for i, k in enumerate(MODEL_PREFERENCE):
                if k in b:
                    return i
            return len(MODEL_PREFERENCE)
        return sorted(dirs, key=lambda d: (score(d), d))[0]
    for d in DEFAULT_TTS_DIRS:
        if find_tts_files(d)[0]:
            return d
    for d in DEFAULT_TTS_DIRS:
        if os.path.isdir(d):
            return d
    return DEFAULT_TTS_DIRS[0]


def find_tts_files(tts_dir):
    """返回 (model, lexicon, tokens, data_dir)；找不到返回 (None,...)。"""
    if not tts_dir or not os.path.isdir(tts_dir):
        return None, None, None, None
    model = None
    for n in MODEL_CANDIDATES:
        p = os.path.join(tts_dir, n)
        if os.path.isfile(p):
            model = p
            break
    if model is None:
        for f in sorted(os.listdir(tts_dir)):
            if f.endswith(".onnx"):
                model = os.path.join(tts_dir, f)
                break
    lex = os.path.join(tts_dir, "lexicon.txt")
    lex = lex if os.path.isfile(lex) else None
    tok = os.path.join(tts_dir, "tokens.txt")
    tok = tok if os.path.isfile(tok) else None
    dd = os.path.join(tts_dir, "dict")
    dd = dd if os.path.isdir(dd) else tts_dir
    return model, lex, tok, dd


# ══════════════════════════════════════════════════════════════════════
#  模型类型识别（sherpa-onnx 支持多种 TTS 架构，配置字段各不一样）
# ══════════════════════════════════════════════════════════════════════

#: 本板 sherpa_onnx 1.13.8 实测支持的 TTS 架构
SUPPORTED_TYPES = ("vits", "kokoro", "matcha")


def _first_in(names, *cands):
    for c in cands:
        if c in names:
            return c
    return None


def _first_glob(names, *pats):
    for p in pats:
        for n in sorted(names):
            if fnmatch.fnmatch(n.lower(), p):
                return n
    return None


def detect_model_type(info, explicit="auto"):
    """
    判断模型目录属于哪种 TTS 架构。

    **为什么要判断**：sherpa-onnx 里 vits / kokoro / matcha 的配置字段完全不同
    （`OfflineTtsVitsModelConfig` / `OfflineTtsKokoroModelConfig` /
    `OfflineTtsMatchaModelConfig`），填错就加载失败。这里靠文件特征自动识别，
    免得每换一个模型都要手改代码。

    特征：
      · kokoro  → 有 `voices.bin` / `voices.txt`（音色表）
      · matcha  → 有 `hifigan*.onnx` 声码器，或 `model-steps-*.onnx`
      · 其余    → 按 vits 处理
    """
    if explicit and explicit != "auto":
        return explicit
    names = [n.lower() for n in (info.get("names") or [])]
    base = os.path.basename((info.get("dir") or "").rstrip("/")).lower()
    if "voices.bin" in names or "voices.txt" in names or "kokoro" in base:
        return "kokoro"
    if any("hifigan" in n for n in names) \
            or any(n.startswith("model-steps") for n in names) \
            or "matcha" in base:
        return "matcha"
    return "vits"


def _pick_rule_fsts(tts_dir, lang=None):
    """
    挑出要加载的文本正则化 FST。

    多语言模型（如 kokoro-multi-lang）目录里会同时有 `date-zh.fst` 和
    `date-en.fst` 等 —— **不能全加载**，否则中文文本会被英文规则处理。
    所以：带语言标记的只取匹配 `lang` 的，不带标记的（`number.fst` 那种）全要。
    """
    try:
        fs = sorted(f for f in os.listdir(tts_dir) if f.endswith(".fst"))
    except Exception:
        return ""
    if not fs:
        return ""
    if lang:
        tagged = [f for f in fs
                  if ("-%s." % lang) in f or ("_%s." % lang) in f]
        plain = [f for f in fs if "-" not in f and "_" not in f]
        sel = tagged + plain
    else:
        sel = fs
    return ",".join(os.path.join(tts_dir, f) for f in sel)


def inspect_tts_dir(tts_dir):
    """
    把一个 TTS 模型目录的"家底"摸清楚（各架构需要的文件不一样）。

    返回 dict；目录不存在返回 {}。`type` 字段是 detect_model_type 的结论。
    """
    if not tts_dir or not os.path.isdir(tts_dir):
        return {}
    try:
        names = sorted(os.listdir(tts_dir))
    except Exception:
        return {}
    eg = os.path.join(tts_dir, "espeak-ng-data")
    dd = os.path.join(tts_dir, "dict")
    lex_zh = _first_in(names, "lexicon-zh.txt")
    info = {
        "dir": tts_dir,
        "names": names,
        "tokens": _first_in(names, "tokens.txt"),
        "lexicon": lex_zh or _first_in(names, "lexicon.txt", "lexicon-us-en.txt"),
        "data_dir": eg if os.path.isdir(eg) else tts_dir,
        "dict_dir": dd if os.path.isdir(dd) else None,
        "voices": _first_in(names, "voices.bin", "voices.txt"),
        "model": _first_glob(names, "*.onnx"),
        "acoustic": _first_glob(names, "model-steps-*.onnx", "model.onnx",
                                "*.onnx"),
        "vocoder": _first_glob(names, "hifigan*.onnx", "*vocoder*.onnx"),
    }
    for k in ("tokens", "lexicon", "voices", "model", "acoustic", "vocoder"):
        if info.get(k):
            info[k] = os.path.join(tts_dir, info[k])
    info["type"] = detect_model_type(info)
    return info


# ══════════════════════════════════════════════════════════════════════
#  引擎
# ══════════════════════════════════════════════════════════════════════

# ── 当前正在播放的子进程（供 `stop_playback()` 掐掉）──────────────────
#: 见 `TtsEngine._run()` / `TtsEngine.stop_playback()`：
#: 用户按 F2 插话时用来**立刻打断播报**，不必等当前那句播完。
_PLAYER_LOCK = threading.Lock()
_CURRENT_PLAYER = None

#: **播放世代**：每次 `stop_playback()` 自增一次。
#:
#: 🔴 为什么需要它（2026-10-11 实测发现的坑）：
#:   `play_samples()` 有一条**失败重试链**（①上次成功的设备 → ②PulseAudio
#:   → ③逐个 ALSA 设备 → ④默认设备）。`stop_playback()` 把当前 aplay 杀掉后，
#:   `_run()` 只是返回 False —— 重试链**会当成"这个设备不行"，换下一个又播一遍
#:   同一段音频**。实测 PID 27043 被杀后立刻冒出 27048 放同一个 wav，
#:   于是"按下 F2 打断播报"完全无效（用户要等到整段播完）。
#:   ⇒ `play_samples()` 在开始前记下世代号，**每次重试前对一次**；
#:     世代变了说明"用户要打断"，立刻放弃整条重试链。
_PLAY_EPOCH = 0
_PLAY_EPOCH_LOCK = threading.Lock()


def playback_epoch():
    """当前播放世代号（见 `_PLAY_EPOCH` 的说明）。"""
    with _PLAY_EPOCH_LOCK:
        return _PLAY_EPOCH


def _bump_play_epoch():
    global _PLAY_EPOCH
    with _PLAY_EPOCH_LOCK:
        _PLAY_EPOCH += 1
        return _PLAY_EPOCH


class TtsEngine(object):
    def __init__(self, log=None):
        self._log = log or (lambda s: None)
        self._tts = None
        self._dir = None
        self._type = "vits"
        self.sample_rate = 16000
        # 合成是**全局串行**的，见 synth() 的说明
        self._gen_lock = threading.Lock()
        # 额外的"输出端"（网络音箱等），见 add_sink()
        self._sinks = []
        # 上次播放成功的 ALSA 设备（如 "plughw:1,0"），后续句子直接用
        self._play_dev = None
        # ★ 昇腾 NPU 后端（若启用并被选中，synth/say 会转发给它）
        self._npu = None
        self.backend = "cpu"        # "cpu"(sherpa) 或 "npu"

    # ── 昇腾 NPU 后端 ───────────────────────────────────────────────
    def enable_npu(self, tts_dir=None, background=False):
        """
        尝试启用**昇腾 NPU** 后端（FastSpeech2 的 5 个 `.om`）。

        返回 `(是否成功, 提示)`。**失败不影响 CPU 后端** —— 会自动继续用 sherpa。

        ⚠️ 只加载模型（不切换）。加载成功后才把 `backend` 置为 `"npu"`。
        `background=True` 时在后台线程加载（本板加载约 2.5 秒，但别冻界面）。
        """
        try:
            import npu_tts_engine as N
        except Exception as e:
            return False, "npu_tts_engine 不可用：%r" % (e,)

        ok, why = N.available()
        if not ok:
            return False, why

        if background:
            def _work():
                try:
                    eng = N.NpuTtsEngine(log=self._log, tts_dir=tts_dir)
                    ok2, msg = eng.load()
                    if ok2:
                        self._npu = eng
                        self.backend = "npu"
                        self.sample_rate = eng.sample_rate
                        # ★ _dir 也要设：GUI 用它显示模型名（走 NPU 时
                        #   TtsEngine.load() 没被调用，不设就会是 None →
                        #   os.path.basename(None) 抛 TypeError）
                        self._dir = getattr(eng, "_dir", None)
                    self._log("NPU TTS：%s" % msg)
                except Exception as e:
                    self._log("NPU TTS 后台加载异常：%r" % (e,))
            threading.Thread(target=_work, daemon=True).start()
            return True, "NPU TTS 正在后台加载…"

        try:
            eng = N.NpuTtsEngine(log=self._log, tts_dir=tts_dir)
            ok2, msg = eng.load()
        except Exception as e:
            return False, "NPU TTS 加载异常：%r" % (e,)
        if not ok2:
            return False, msg
        self._npu = eng
        self.backend = "npu"
        self.sample_rate = eng.sample_rate
        self._dir = getattr(eng, "_dir", None)   # 见上：GUI 要显示模型名
        return True, msg

    def disable_npu(self):
        """关掉 NPU 后端，退回 sherpa（CPU）。"""
        npu, self._npu = self._npu, None
        self.backend = "cpu"
        if npu is not None:
            try:
                npu.close()
            except Exception:
                pass
        return True

    @property
    def npu_status(self):
        """NPU 后端状态（供 GUI / 诊断显示）。"""
        if self._npu is None:
            return {"enabled": False, "backend": self.backend}
        try:
            st = self._npu.status()
        except Exception as e:
            return {"enabled": True, "backend": self.backend, "error": repr(e)}
        st["enabled"] = True
        return st

    @property
    def ready(self):
        return (self._npu is not None) or (self._tts is not None)

    @property
    def num_speakers(self):
        if self._npu is not None and self.backend == "npu":
            return self._npu.num_speakers
        try:
            return self._tts.num_speakers
        except Exception:
            return 0

    def load(self, tts_dir=None, num_threads=2, provider="cpu",
             model_type="auto", lang=None):
        """
        加载 TTS 模型。返回 (是否成功, 提示)。

        · `tts_dir=None` → 自动挑；**装了好几个时按 `MODEL_PREFERENCE` 取音质最好的**。
        · `model_type="auto"` → 按目录文件特征自动识别 vits / kokoro / matcha。
        · `lang` → 文本正则化 FST 的语言过滤（kokoro 多语言模型默认按 "zh"）。
        """
        if not _HAS_SHERPA:
            return False, "未安装 sherpa_onnx：%s" % _SH_ERROR
        tts_dir = tts_dir or guess_tts_dir()
        info = inspect_tts_dir(tts_dir)
        if not info:
            return False, "模型目录不存在：%s" % tts_dir
        mtype = detect_model_type(info, model_type)
        if not info.get("tokens"):
            return False, "模型目录 %s 缺少 tokens.txt" % tts_dir

        nt = int(num_threads)
        data_dir = info.get("data_dir") or tts_dir
        rule = _pick_rule_fsts(tts_dir, lang or ("zh" if mtype == "kokoro" else None))

        try:
            if mtype == "kokoro":
                if not info.get("voices"):
                    return False, "kokoro 模型缺少 voices.bin/voices.txt：%s" % tts_dir
                sub = _sherpa.OfflineTtsKokoroModelConfig(
                    model=info["model"], voices=info["voices"],
                    tokens=info["tokens"], data_dir=data_dir,
                    dict_dir=info.get("dict_dir"), lexicon=info.get("lexicon"),
                    lang=(lang or "zh"))
            elif mtype == "matcha":
                if not info.get("vocoder"):
                    return False, "matcha 模型缺少声码器（hifigan*.onnx）：%s" % tts_dir
                sub = _sherpa.OfflineTtsMatchaModelConfig(
                    acoustic_model=info["acoustic"], vocoder=info["vocoder"],
                    tokens=info["tokens"], lexicon=info.get("lexicon"),
                    data_dir=data_dir, dict_dir=info.get("dict_dir"))
            else:
                sub = _sherpa.OfflineTtsVitsModelConfig(
                    model=info["model"], lexicon=info.get("lexicon"),
                    tokens=info["tokens"], data_dir=data_dir,
                    dict_dir=info.get("dict_dir"))
        except Exception as e:
            return False, "构建 %s 配置失败：%s" % (mtype, e)

        key = {"kokoro": "kokoro", "matcha": "matcha"}.get(mtype, "vits")
        mc = _sherpa.OfflineTtsModelConfig(
            **{key: sub, "num_threads": nt, "provider": provider})
        cfg = _sherpa.OfflineTtsConfig(
            model=mc, rule_fsts=rule, max_num_sentences=1)

        t0 = time.time()
        try:
            self._tts = _sherpa.OfflineTts(cfg)
        except Exception as e:
            self._tts = None
            return False, "加载 TTS 失败：%s" % e
        self._dir = tts_dir
        self._type = mtype
        self.sample_rate = self._tts.sample_rate
        return True, "加载完成 %.2f 秒 [%s]，%d 个说话人，%d Hz" % (
            time.time() - t0, mtype, self.num_speakers, self.sample_rate)

    def load_best(self, prefer_npu=True, background=False, tts_dir=None,
                  npu_dir=None, **kw):
        """
        **按优先级加载最好的可用后端**（推荐入口）。

        顺序：**NPU（若 prefer_npu 且可用）→ sherpa/CPU**。
        返回 `(后端名, 是否成功, 提示)`；`后端名` 是 `"npu"` / `"cpu"` / `""`。

        设计意图：NPU 是加速项、**不是依赖**。NPU 不可用（没 om / 没网络 /
        CANN 环境不对）时自动退回 sherpa，行为与以前完全一致。
        """
        env = os.environ.get("CAR_TTS_BACKEND", "").strip().lower()
        if env in ("cpu", "sherpa", "off"):
            prefer_npu = False
        elif env in ("npu", "ascend"):
            prefer_npu = True

        npu_msg = ""
        if prefer_npu:
            ok, npu_msg = self.enable_npu(tts_dir=npu_dir, background=background)
            if ok and not background:
                return "npu", True, npu_msg
            if ok and background:
                # 后台加载 NPU 的同时先把 CPU 备好（NPU 好了会自动切）
                self._log("NPU TTS：后台加载中，先按 CPU 后端就绪")
            elif not ok:
                self._log("NPU TTS 不可用，回退 CPU：%s" % npu_msg)

        ok, msg = self.load(tts_dir=tts_dir, **kw)
        if ok:
            return "cpu", True, msg if not npu_msg else "%s（NPU 不可用：%s）" % (
                msg, npu_msg)
        return "", False, msg

    def voice_names(self):
        """
        音色名列表（kokoro 的 `voices.txt` 一行一个）。
        vits 类模型没有这个文件 → 返回 []，只能用数字编号。
        """
        p = os.path.join(self._dir or "", "voices.txt")
        if not os.path.isfile(p):
            return []
        try:
            with open(p, encoding="utf-8") as f:
                return [l.strip() for l in f
                        if l.strip() and not l.strip().startswith("#")]
        except Exception:
            return []

    # ── 合成 ────────────────────────────────────────────────────────
    def synth(self, text, speaker=0, speed=1.0):
        """
        合成一句话，返回 (采样率, float32 数组) 或 (0, None)。

        ⚠️ **全局串行**（`_gen_lock`）。两个原因：
          ① onnxruntime 的 session **不保证并发安全**；
          ② 本板只有 4 核 1.0GHz —— 两个请求同时合成只会互相抢 CPU、双双变慢，
             排队反而更快。
        实测单句 0.5~5 秒，所以串行的代价很小，换来的是稳定。
        """
        if not self.ready or np is None:
            return 0, None
        # ★ NPU 后端优先（用户显式启用且加载成功时）
        if self._npu is not None and self.backend == "npu":
            return self._npu.synth(text, speaker, speed)
        with self._gen_lock:
            try:
                a = self._tts.generate(text, sid=int(speaker),
                                       speed=float(speed))
                return a.sample_rate, np.array(a.samples, dtype=np.float32)
            except Exception as e:
                self._log("合成失败：%s" % e)
                return 0, None

    def _write_wav(self, path, sr, data):
        import wave
        pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with wave.open(path, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(int(sr))
            f.writeframes(pcm.tobytes())
        return True

    def save(self, text, path, speaker=0, speed=1.0):
        """合成并写成 16bit 单声道 wav。返回 (是否成功, 提示)。"""
        sr, data = self.synth(text, speaker, speed)
        if data is None:
            return False, "合成失败"
        self._write_wav(path, sr, data)
        return True, "%s（%.2f 秒）" % (path, len(data) / float(sr))

    # ── 播放 ────────────────────────────────────────────────────────
    @staticmethod
    def _run(cmd, timeout=60):
        """跑一个播放命令，**并登记它**，以便用户插话时能立刻掐掉。

        ⚠️ 为什么要登记（2026-09-30 用户反馈"等播报结束太久了"）：
        本板播放是起 `aplay` **子进程**放 wav 的。用户按 F2 想说话时，
        麦克风要等这个子进程退出才放得开 —— 实测一条短播报也要等
        **约 1 秒**（当前分句剩余播放 + aplay 排空）。
        用户按 F2 的语义就是"我要说话了"，**打断正在响的那句是自然的**
        （就像人被打断一样）。所以这里记住子进程，供 `stop_playback()` 掐掉。
        """
        global _CURRENT_PLAYER
        if not shutil.which(cmd[0]):
            return False, "找不到命令 %s" % cmd[0]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        except Exception as e:
            return False, str(e)
        with _PLAYER_LOCK:
            _CURRENT_PLAYER = p
        try:
            try:
                out = p.communicate(timeout=timeout)[0] or b""
            except subprocess.TimeoutExpired:
                p.kill()
                out = p.communicate()[0] or b""
                return False, "播放超时"
            if p.returncode == 0:
                return True, ""
            txt = out.decode("utf-8", "replace").strip()
            return False, (txt[:200] or "退出码 %d" % p.returncode)
        finally:
            with _PLAYER_LOCK:
                if _CURRENT_PLAYER is p:
                    _CURRENT_PLAYER = None

    @staticmethod
    def stop_playback():
        """**立刻掐掉正在播放的音频**（用户按 F2 插话时用）。返回是否掐了。

        见 `_run()` 的说明：用户要说话时不必等当前那句播完。

        ★ 同时**递增播放世代**（见 `_PLAY_EPOCH`）：杀掉当前 aplay 会让
          `_run()` 返回 False，而 `play_samples()` 的失败重试链会把
          "被杀掉"误当成"这个设备不行"，**换下一个设备再播一遍同一段** ——
          那样打断就失效了。世代号让重试链知道"这是用户要打断，别再重试"。
        """
        global _CURRENT_PLAYER
        _bump_play_epoch()
        with _PLAYER_LOCK:
            p = _CURRENT_PLAYER
        if p is None or p.poll() is not None:
            return False
        try:
            p.terminate()
        except Exception:
            try:
                p.kill()
            except Exception:
                return False
        return True

    def play_samples(self, sr, data, device=None, sink=None):
        """
        播放一段**已在内存里**的音频。返回 (是否成功, 提示)。

        device: aplay 的设备，如 "plughw:1,0"
        sink:   PulseAudio 的 sink 名（给 paplay -d）
        都不给则自动挑。

        ⚠️ **自动挑的顺序很重要**（2026-09-27 实机踩到）：
        先试**显式设备** `aplay -D plughw:N,M`，**最后**才试"默认设备"。
        因为很多板子上 ALSA 的 `default` 指向 card 0，而 card 0 可能压根没有
        播放端点（本板接的 PCM2902 就是纯采集）→ "默认设备"必然失败。
        把它放前面只会白等一次超时，还把错误信息搅浑，看不出真正原因。

        成功过的设备会被记住，后面每句直接用，不再重复试错。
        """
        import tempfile
        if data is None or len(data) == 0:
            return False, "没有音频数据"

        probe = output_probe()
        if not probe["ok"]:
            return False, NO_OUTPUT_HINT
        if not probe["players"]:
            return False, "系统里没有 paplay / aplay / play，无法播放"

        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="tts_")
        os.close(fd)
        # ★ 记下"播放世代"：用户按 F2 打断会递增它（见 `_PLAY_EPOCH`）。
        #   每次重试前对一次 —— 不然被打断会被误当成"这个设备不行"，
        #   换下一个设备**把同一段又播一遍**，打断就失效了。
        epoch = playback_epoch()

        def _cancelled():
            return playback_epoch() != epoch

        try:
            self._write_wav(tmp, sr, data)

            if sink:
                ok, msg = self._run(["paplay", "-d", sink, tmp])
                return (True, "已播放（paplay → %s）" % sink) if ok else (False, msg)
            if device:
                ok, msg = self._run(["aplay", "-D", device, tmp])
                return (True, "已播放（aplay → %s）" % device) if ok else (False, msg)

            tried = []

            # ① 上次成功的设备，直接用（省掉每句一次的试错）
            if self._play_dev:
                ok, msg = self._run(["aplay", "-D", self._play_dev, tmp])
                if ok:
                    return True, "已播放（aplay -D %s）" % self._play_dev
                tried.append("aplay -D %s: %s" % (self._play_dev, msg))
                self._play_dev = None
                if _cancelled():
                    return False, "已被打断（放弃重试）"

            # ② PulseAudio（只有看得见 sink 才试；本板 PA 跑在 sddm 会话下，
            #    SSH 里的 HwHiAiUser 多半连不上，会报 Connection refused）
            if shutil.which("paplay") and probe["sinks"]:
                if _cancelled():
                    return False, "已被打断（放弃重试）"
                ok, msg = self._run(["paplay", tmp])
                if ok:
                    return True, "已播放（paplay，PulseAudio 默认 sink）"
                tried.append("paplay: %s" % msg)

            # ③ 显式 ALSA 设备（最可靠的那条）
            if shutil.which("aplay"):
                for d in probe["alsa"]:
                    if _cancelled():
                        return False, "已被打断（放弃重试）"
                    ok, msg = self._run(["aplay", "-D", d["id"], tmp])
                    if ok:
                        self._play_dev = d["id"]
                        return True, "已播放（aplay -D %s / %s）" % (
                            d["id"], d.get("name", ""))
                    tried.append("aplay -D %s: %s" % (d["id"], msg))
                # ④ 最后才试默认设备
                if _cancelled():
                    return False, "已被打断（放弃重试）"
                ok, msg = self._run(["aplay", tmp])
                if ok:
                    return True, "已播放（aplay 默认设备）"
                tried.append("aplay: %s" % msg)

            return False, "播放失败：%s" % "；".join(tried)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def play(self, text, speaker=0, speed=1.0, device=None, sink=None):
        """
        合成并播放**整段**文本。需要系统有输出设备。
        长文本建议改用 `say()`（分句流式，首句出声更快）。
        """
        sr, data = self.synth(text, speaker, speed)
        if data is None:
            return False, "合成失败"
        return self.play_samples(sr, data, device=device, sink=sink)

    # ── 额外输出端（sink）──────────────────────────────────────────
    def add_sink(self, fn):
        """
        注册一个额外的"输出端"：`fn(rate, data, text) -> None`。

        **为什么需要它（本板的救命设计）**：本板**没有本地播放设备**
        （见文件头的三条硬件死路）。如果 `say()` 只能在本地放，
        那接进小车后就是"合成完直接丢掉"，永远听不到。

        有了 sink，`say()` 的语义变成：
            **本地能放就放，同时把音频送给所有 sink**
        而不是"本地放不了就整句丢掉"。`tts_serve.py` 就是靠它把播报
        送到浏览器的（`start_background(engine)`）。
        """
        if fn not in self._sinks:
            self._sinks.append(fn)
        # ★ 同步给 NPU 后端，否则切到 NPU 后 sink 就断了（网络音箱收不到）
        if self._npu is not None:
            try:
                self._npu.add_sink(fn)
            except Exception:
                pass
        return fn

    def remove_sink(self, fn):
        try:
            self._sinks.remove(fn)
            ok = True
        except ValueError:
            ok = False
        if self._npu is not None:
            try:
                self._npu.remove_sink(fn)
            except Exception:
                pass
        return ok

    def say(self, text, speaker=0, speed=1.0, device=None, sink=None,
            should_stop=None):
        """
        分句合成 + 边合成边播（首句更快出声）。返回 (是否成功, 提示)。

        这是给小车用的主接口。输出策略：

            · 有本地播放设备 → 本地播；
            · 注册了 sink（网络音箱）→ 同时送给它们；
            · 两者都没有 → 返回 NO_OUTPUT_HINT，但**不抛异常** ——
              播报失败绝不能影响车的动作。

        `should_stop`：可选的 `() -> bool` 回调，**每个分句前问一次**；
        返回 True 就停止播剩下的（供 `SpeakQueue.preempt()` 抢占用，
        见那里的说明：用户按 F2 要说话时不必把整条播报说完）。
        """
        sents = split_sentences(text)
        if not sents:
            return False, "文本为空"
        # ★ NPU 后端：它自己会再分一次句（上限更严，见 npu_tts_engine.say）
        if self._npu is not None and self.backend == "npu":
            return self._npu.say(text, speaker=speaker, speed=speed,
                                 device=device, sink=sink,
                                 should_stop=should_stop)

        probe = output_probe()
        if not probe["ok"] and not self._sinks:
            return False, NO_OUTPUT_HINT

        done, errs = 0, []
        for i, s in enumerate(sents):
            # ★ 抢占检查：用户要说话了，别再为"听不到的后几句"白等（见 preempt()）
            if should_stop is not None and should_stop():
                errs.append("已被抢占，跳过剩余 %d 句" % (len(sents) - i))
                break
            sr, data = self.synth(s, speaker, speed)
            if data is None:
                errs.append("第 %d 句合成失败" % (i + 1))
                continue
            # ★★ 合成完成后**再查一次**抢占（2026-10-11）：
            #   合成一句要 ~1.11s，这期间用户很可能已经按下 F2。
            #   此时这一句**绝不能播出去** —— 否则麦克风刚要打开又被播报占住
            #   （本板播放/采集硬件互斥），用户看到的就是"录音启动失败"。
            #   注意 `_preempted()` 取出即清空，所以这里查一次是安全的。
            if should_stop is not None and should_stop():
                errs.append("已被抢占（合成后丢弃第 %d 句）" % (i + 1))
                break
            delivered = False
            for fn in list(self._sinks):
                try:
                    fn(sr, data, s)
                    delivered = True
                except Exception as e:
                    errs.append("sink 异常：%s" % e)
            if probe["ok"]:
                ok, msg = self.play_samples(sr, data, device=device, sink=sink)
                if ok:
                    delivered = True
                else:
                    # 报错别截太狠：ALSA 的第一行往往只是"试了哪条路"，
                    # 真正有用的信息（比如设备不存在）在后面
                    errs.append("第 %d 句播放失败：%s" % (
                        i + 1, (msg or "").splitlines()[0][:130]))
            if delivered:
                done += 1

        if done == len(sents):
            how = "本地播放" if probe["ok"] else "已送到 %d 个输出端" % len(self._sinks)
            return True, "已播报 %d 句（%s）" % (done, how)
        if done:
            return True, "已播报 %d/%d 句；%s" % (done, len(sents), "；".join(errs))
        return False, "；".join(errs) or "播报失败"

    # ── 音量 ────────────────────────────────────────────────────────
    @staticmethod
    def set_volume(percent, card=None, control="Speaker"):
        """
        设置输出音量。card=None 时对所有播放卡片都试一遍。
        返回 [(card, 是否成功, 提示)]。
        """
        if not shutil.which("amixer"):
            return []
        cards = [card] if card is not None else \
            sorted({d["card"] for d in list_output_devices()})
        out = []
        for c in cards:
            ok, msg = TtsEngine._run(
                ["amixer", "-c", str(c), "sset", control, "%d%%" % int(percent)])
            out.append((c, ok, "" if ok else msg))
        return out


class SpeakQueue(object):
    """
    后台串行播报队列 —— 接到小车里用的就是它。

    两条铁律（和 ASR / KWS 的教训完全一致）：

      ① **播报绝不能阻塞主线程** —— 合成要 1~4 秒，直接调会让 GUI 冻住；
      ② **播报之间不能重叠** —— 两句同时出声就听不清了。

    所以用一个常驻后台线程 + 队列串行消费：谁想说话就往里 `put()`，
    立刻返回，不等待。

        sq = SpeakQueue(tts)
        sq.put("已急停")          # 立即返回
        ...
        sq.close()
    """

    def __init__(self, tts, on_done=None, mic_pause=None, mic_resume=None,
                 **say_kw):
        self._tts = tts
        self._q = _Queue()
        self._kw = say_kw
        self._on_done = on_done or (lambda ok, msg, text: None)
        #: ★ 播报前/后让出麦克风的回调（2026-10-11）。
        #:   本板采集与播放**双向互斥**（同一 xHCI + 全速 USB 带宽不足）：
        #:   麦克风被 KWS 常驻占着时 `aplay` 报
        #:   `set_params:1416: Unable to install hw params` **必然失败**。
        #:   所以在每条播报前后调用这两个钩子（由 CarController 提供，
        #:   它会暂停/恢复 KWS）。对急停**零损失** —— 播报期间麦克风本来也打不开。
        self._mic_pause = mic_pause
        self._mic_resume = mic_resume
        self._running = True
        self._busy = False
        #: 抢占标志（见 `preempt()`）—— 用户要说话时打断播报用
        self._preempt = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def preempt(self):
        """**抢占播报**：丢掉排队的，并让正在说的那句**说完当前分句就停**。

        🔴 为什么需要它（2026-09-30 用户反馈"等播报结束太久、不利于连续指令"）：
        分句播报是**串行**的，而本板每句合成要 ~1.11s。实测一条被切成 4 句的
        播报：用户听到最后一声之后，系统还在为**后面几句**做合成，
        界面就一直显示「⏳ 等播报结束…」—— 这段完全可以不等。

        ⇒ 用户按 F2 要说话时**不必把剩下的播报说完**：
          丢掉还没合成/没播的，正在播的那一句也**播完当前分句就收工**。
        于是"按下 F2"到"麦克风可用"只剩当前分句的剩余播放时间
        （通常 <1s），而不是"整条播报剩余时长"。

        ⚠️ 安全语义不受影响：急停是**先下发停车帧、再异步播报**的
        （见 `trigger_emergency`），抢占最多截断"已急停"的**尾音**，
        绝不会影响停车本身。

        🔴🔴 **只在真的在播时才武装标志**（2026-10-11 修用户报的
        「偶尔播报被秒吞、语音输出没了」）：
        `_preempted()` 是**取出即清空**的，而它**只在 `say()` 的分句循环里**
        被调用。所以如果抢占时队列是空闲的（`_busy == False`），
        这个标志就会**一直留在那儿**，然后被**下一条播报**消费掉 ——
        表现就是：用户按 F2 说完一句，小车回应的「开始前进」**一开口就被
        当成"已被抢占"掐掉**，声音完全没了。
        ⇒ 没在播就**不要武装**，顺手把标志清干净。
        """
        dropped = self.clear_pending()
        if self._busy:
            self._preempt = True
        else:
            self._preempt = False
        return dropped

    def _preempted(self):
        """取出并清空抢占标志（播报循环在每个分句前查它）。"""
        if self._preempt:
            self._preempt = False
            return True
        return False

    @property
    def speaking(self):
        """
        当前是否**正在合成/播放**。

        ⚠️ 和 `pending()` 不是一回事：`pending()==0` 只说明"排队里没有新条目"，
        此刻很可能正在合成或播放上一条。要判断"说完了没有"必须同时看这两个。
        """
        return self._busy

    def idle(self):
        """队列空且当前没在说 —— 也就是"彻底安静了"。"""
        return (not self._busy) and self._q.qsize() == 0

    def put(self, text, drop_pending=False):
        """
        投递一句播报，立即返回队列长度。

        `drop_pending=True` 会**先清空排队中的播报**，用于两类场景：

          · **动作播报"过期作废"**：车已经停了，还在慢慢说"开始前进"
            毫无意义 —— 本板 RTF ≈ 5，排队滞后的播报只会误导人；
          · **急停播报要顶掉所有排队内容**。
        """
        if drop_pending:
            self.clear_pending()
        self._q.put(text)
        return self._q.qsize()

    def clear_pending(self):
        """丢掉所有**还没开始合成**的播报。正在说的那一句不会被打断。"""
        n = 0
        while True:
            try:
                self._q.get_nowait()
                n += 1
            except _Empty:
                break
        return n

    def pending(self):
        return self._q.qsize()

    def _loop(self):
        while self._running:
            try:
                text = self._q.get(timeout=0.3)
            except _Empty:
                continue
            if text is None:
                break
            self._busy = True
            paused = False
            try:
                # ★★ 播报前让出麦克风（2026-10-11）：
                #    本板采集与播放双向互斥，KWS 常驻占着麦克风时
                #    `aplay` 必然 `set_params:1416: Unable to install hw params`。
                #    由 CarController 提供的回调去暂停 KWS。
                if self._mic_pause is not None:
                    try:
                        paused = bool(self._mic_pause())
                    except Exception:
                        paused = False
                # ★ 把"是否被抢占"告诉 say()，让它每个分句前自检
                #   （见 `preempt()`：用户要说话时不必把整条播报说完）
                ok, msg = self._tts.say(text, should_stop=self._preempted,
                                        **self._kw)
            except Exception as e:
                ok, msg = False, str(e)
            finally:
                self._busy = False
                # ★ 播报结束后恢复麦克风（放在 `_busy=False` 之后：
                #   此刻播报已经真的结束了，恢复监听才是安全的）
                if paused and self._mic_resume is not None:
                    try:
                        self._mic_resume()
                    except Exception:
                        pass
            try:
                self._on_done(ok, msg, text)
            except Exception:
                pass

    def close(self):
        """
        停止队列。

        ⚠️ **不用 sentinel 唤醒**：`clear_pending()` 可能把哨兵一起丢掉，
        那样线程就永远退不出来了。改为靠 `_running` 标志 ——
        工作线程最多 0.3 秒后自然退出（正在合成的那一句会说完）。
        """
        self._running = False


# ══════════════════════════════════════════════════════════════════════
#  诊断
# ══════════════════════════════════════════════════════════════════════

def dependency_status():
    st = {"numpy": np is not None, "sherpa_onnx": _HAS_SHERPA,
          "error": _SH_ERROR, "output_devices": ""}
    if _HAS_SHERPA:
        st["version"] = getattr(_sherpa, "__version__", "?")
    try:
        p = subprocess.run(["aplay", "-l"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=3)
        st["output_devices"] = p.stdout.decode("utf-8", "replace").strip() \
            or "（无）"
    except Exception as e:
        st["output_devices"] = "查询失败：%s" % e
    return st


def print_check():
    """
    环境诊断。**故意不加载模型** —— 诊断工具不能被它要诊断的问题卡住
    （这个教训在 KWS 那边已经踩过一次，见《12.KWS…》§4.7）。
    """
    print("=" * 70)
    print("  TTS 环境诊断")
    print("=" * 70)
    print("  解释器        = %s" % sys.executable)
    print("  Python        = %d.%d.%d" % sys.version_info[:3])
    print("  numpy         = %s" % ("✅" if np is not None else "❌ 缺失"))
    print("  sherpa_onnx   = %s" % (
        ("✅ v%s" % getattr(_sherpa, "__version__", "?")) if _HAS_SHERPA
        else "❌ 缺失 %s" % _SH_ERROR))

    dirs = list_tts_dirs()
    d = guess_tts_dir()
    info = inspect_tts_dir(d)
    print()
    print("  可用 TTS 模型 = %d 个（✓ = 自动选中）" % len(dirs))
    for x in dirs:
        print("     %s %-38s [%s]" % (
            "✓" if x == d else " ", os.path.basename(x),
            inspect_tts_dir(x).get("type", "?")))
    if not dirs:
        print("     （无 —— 跑 download_tts_model.sh）")
    print("  架构          = %s" % info.get("type", "?"))
    for label, key in (("model", "model"), ("vocoder", "vocoder"),
                       ("voices", "voices"), ("lexicon", "lexicon"),
                       ("tokens", "tokens")):
        p = info.get(key)
        if p:
            print("    %-9s   = ✅ %s" % (label, os.path.basename(p)))
        elif key == "tokens" or (key == "vocoder" and info.get("type") == "matcha"):
            print("    %-9s   = ❌ 缺失" % label)
    for f in ("number.fst", "date.fst", "phone.fst"):
        if os.path.isfile(os.path.join(d, f)):
            print("    %-9s   = ✅" % f)

    print()
    print("  ── 输出侧 ──")
    probe = output_probe()
    print("  播放器命令    = %s" % (", ".join(probe["players"]) or "无"))
    print("  ALSA 播放设备 = %d 个" % len(probe["alsa"]))
    for x in probe["alsa"]:
        print("     %-14s card %d / device %d   %s" % (
            x["id"], x["card"], x["device"], x["name"]))
    print("  PA/PW sinks   = %d 个%s" % (
        len(probe["sinks"]),
        "（root/SSH 下取不到属正常，切到桌面用户再看）"
        if not probe["sinks"] else ""))
    for s in probe["sinks"]:
        print("     %s" % s["id"])
    print("  能否播放      = %s" % ("✅ 有输出设备" if probe["ok"] else "❌ 无"))

    print()
    if probe["ok"]:
        print("  结论：有输出设备，可以直接播报：")
        print("     bash run_py.sh tts_engine.py --text \"已急停\" --play")
    else:
        print(NO_OUTPUT_HINT)
        print()
        print("  现在就能用的替代方案（把浏览器当喇叭）：")
        print("     bash run_py.sh tts_serve.py --port 8099")
        print("     PC/手机浏览器打开  http://<板子IP>:8099/")
    print("=" * 70)
    return 0 if probe["ok"] else 1


# ══════════════════════════════════════════════════════════════════════
#  命令行
# ══════════════════════════════════════════════════════════════════════

def _main():
    import argparse
    ap = argparse.ArgumentParser(description="中文 TTS（sherpa-onnx VITS）")
    ap.add_argument("--text", default="已急停")
    ap.add_argument("--out", default=None, help="输出 wav 路径")
    ap.add_argument("--speaker", type=int, default=None,
                    help="说话人编号（默认用该模型实测好用的那个，见 --list-speakers）")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--dir", default=None, help="TTS 模型目录（默认自动挑最好的）")
    ap.add_argument("--model-type", default="auto",
                    choices=("auto",) + SUPPORTED_TYPES, help="模型架构（默认自动识别）")
    ap.add_argument("--lang", default=None, help="文本正则化语言（kokoro 多语言模型）")
    ap.add_argument("--list-models", action="store_true",
                    help="列出所有可用的 TTS 模型（按音质优先级排序）")
    ap.add_argument("--play", action="store_true", help="合成并播放（需输出设备）")
    ap.add_argument("--device", default=None, help="aplay 设备，如 plughw:1,0")
    ap.add_argument("--sink", default=None, help="PulseAudio sink 名（paplay -d）")
    ap.add_argument("--volume", type=int, default=None, help="播放前设置音量%%")
    ap.add_argument("--check", action="store_true",
                    help="环境诊断（快，不加载模型）")
    ap.add_argument("--list-devices", action="store_true", help="列出播放设备")
    ap.add_argument("--list-speakers", action="store_true")
    args = ap.parse_args()

    # 诊断类动作放最前面，不受缺依赖 / 缺硬件影响
    if args.check:
        return print_check()

    if args.list_models:
        dirs = list_tts_dirs()
        if not dirs:
            print("没找到任何可用的 TTS 模型（扫描目录：%s）"
                  % "、".join(TTS_ROOT_CANDIDATES))
            return 1
        chosen = guess_tts_dir()
        print("发现 %d 个 TTS 模型（✓ = 自动选中）：" % len(dirs))
        for d in dirs:
            info = inspect_tts_dir(d)
            mark = "✓" if d == chosen else " "
            print("  %s %-38s [%s]  %s" % (
                mark, os.path.basename(d), info.get("type", "?"),
                "音色表 voices.bin" if info.get("voices") else "无音色表（用编号）"))
            print("      %s" % d)
        print()
        print("指定用哪个：--dir <路径>")
        return 0

    if args.list_devices:
        probe = output_probe()
        print("ALSA 播放设备（共 %d）：" % len(probe["alsa"]))
        for x in probe["alsa"]:
            print("   %-14s card %d device %d  %s" % (
                x["id"], x["card"], x["device"], x["name"]))
        if not probe["alsa"]:
            print("   （无）")
        print("PA/PW sinks（共 %d）：%s" % (
            len(probe["sinks"]), ", ".join(s["id"] for s in probe["sinks"])
            or "（无 / 取不到）"))
        print("播放器命令：%s" % (", ".join(probe["players"]) or "无"))
        return 0

    dep = dependency_status()
    print("依赖：numpy=%s sherpa_onnx=%s(%s)" % (
        dep["numpy"], dep["sherpa_onnx"], dep.get("version", "-")))

    eng = TtsEngine(log=lambda s: print("  [TTS] %s" % s))
    ok, msg = eng.load(args.dir, args.threads,
                       model_type=args.model_type, lang=args.lang)
    print("加载：", ok, msg)
    if not ok:
        return 1

    if args.speaker is None:
        args.speaker = default_speaker(eng._dir)

    if args.list_speakers:
        voices = eng.voice_names()
        print("模型：%s  架构：%s" % (os.path.basename(eng._dir), eng._type))
        print("共 %d 个说话人（编号 0 ~ %d）" % (
            eng.num_speakers, max(0, eng.num_speakers - 1)))
        rng = zh_speaker_range(eng._dir)
        if rng:
            print()
            print("  ⚠️  这是**多语言**模型，编号里混着各国语言的音色 ——")
            print("       **编号 0 是英语音色**，拿它念中文会带很重的英文口音。")
            print("       实测能正常读中文的是 **%d ~ %d**；默认用 %d。"
                  % (rng[0], rng[1], args.speaker))
            print()
        if voices:
            print("音色名（voices.txt，共 %d）：" % len(voices))
            for i, v in enumerate(voices):
                print("  %3d %-16s" % (i, v), end="")
                if i % 3 == 2:
                    print()
            print()
        else:
            sp = os.path.join(eng._dir, "speakers.txt")
            if os.path.isfile(sp):
                for i, line in enumerate(open(sp, encoding="utf-8")):
                    if i % 6 == 0:
                        print()
                    print("  %3d %-10s" % (i, line.strip()), end="")
                print()
        return 0

    if args.volume is not None:
        for c, vok, vmsg in TtsEngine.set_volume(args.volume):
            print("  音量 card %d → %s %s" % (
                c, "✅" if vok else "❌", vmsg))

    if args.play:
        ok, msg = eng.say(args.text, args.speaker, args.speed,
                          device=args.device, sink=args.sink)
        print("播放：", ok)
        print(msg)
        return 0 if ok else 1

    out = args.out or "/tmp/tts_%s.wav" % int(time.time())
    t0 = time.time()
    ok, msg = eng.save(args.text, out, args.speaker, args.speed)
    print("合成（%.2fs）：" % (time.time() - t0), ok, msg)
    if ok:
        print()
        print("两种听法：")
        print("  ① 传回 PC 播：scp root@<板子IP>:%s ./" % out)
        print("  ② 起网络音箱：bash run_py.sh tts_serve.py")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
