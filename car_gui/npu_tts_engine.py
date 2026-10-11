#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
npu_tts_engine.py —— 昇腾 NPU 上的中文语音合成（FastSpeech2）

## 定位

`tts_engine.TtsEngine` 的**同接口替代实现**，底层从 sherpa-onnx（CPU, Kokoro）
换成**昇腾 NPU**（FastSpeech2 的 5 个 `.om`）。

之所以能做到"同接口"，是因为整车链路只依赖这几个方法：
    `ready` / `sample_rate` / `num_speakers` / `load()` / `synth()` / `say()`
    （`SpeakQueue` 只调 `say()`，见 `tts_engine.py`）
只要这些签名一致，**`SpeakQueue` 与全部安全链路一行都不用改**。

## 实测性能（2026-09-30，Atlas 200I DK A2 / Ascend310B4）

| 文本 | 合成耗时 | 音频时长 | RTF |
|---|---|---|---|
| 大家好 | 1.11s | 0.81s | 1.37 |
| 你好，我是你的智能小车 | 1.11s | 2.38s | 0.46 |
| 检测到障碍物，请小心 | 1.11s | 2.28s | 0.49 |

**平均 RTF 0.92（达到实时）**，对比 Kokoro(CPU) 的 RTF 5.43 **快 5.89 倍**。

## ⚠️ 两条必须知道的特性

1. **合成耗时恒定约 1.11s，与句子长短无关** —— 因为 310B **只支持静态 shape**，
   模型永远按 `max_mel_len=250` 帧（≈2.9 秒音频）算满。
   → **短句 RTF > 1**（"大家好"1.37），长句 < 1。**别指望"短句更快"。**

2. **有硬上限，超了会静默截断**：
   · `max_src_len = 64` 音素（≈25~30 个汉字）
   · `max_mel_len = 250` 帧（≈2.9 秒音频）
   超限时官方代码会打印 `Truncate the input` 并**丢掉后面的话**。
   → `say()` 里用 `tts_engine.split_sentences()` 先分句规避；分句后仍超限会被截断。

## 依赖

- `/usr/local/Ascend` 的 CANN（pyACL）+ ATC 转好的 5 个 `.om`
- `car_gui/acl_infer_session.py`（pyACL 版 InferSession）
- `car_gui/torch.py`（极简 numpy 替身，本板装不动真 PyTorch）
- `car_gui/ais_bench/`（转发到上面那个 InferSession，让官方代码零改动）
- `car_gui/npu_tts/`（官方 synthesize.py / utils.py / text/，**未改动**）

## 用法

    from npu_tts_engine import NpuTtsEngine
    eng = NpuTtsEngine()
    ok, msg = eng.load()
    sr, data = eng.synth("大家好")      # -> (22050, float32[-1,1])
    eng.say("大家好")                    # 分句 + 送 sink/本地播放
"""
import os
import shutil
import subprocess
import sys
import threading
import time

# ══════════════════════════════════════════════════════════════════════
# ★★★ 必须在 `import numpy` **之前**执行：把工程自带的 tbe 依赖目录
#      (`npu_libs/`，内含 **numpy 1.26** + scipy 等) 插到 `sys.path` 最前。
#
# 为什么顺序如此关键（实测踩过）：
#   官方 `synthesize.py` → `utils.py` → `import scipy` 这条链需要 scipy，
#   而 npu_libs 里的 scipy 是**用 NumPy 1.x 编译的**，遇到 NumPy 2.x 会直接崩：
#       "A module that was compiled using NumPy 1.x cannot be run in NumPy 2.2.6"
#   本模块早期在顶部先 `import numpy`（拿到系统的 **2.2.6**），之后才插
#   sys.path —— 此时 numpy 已在 `sys.modules` 里，后面 `import scipy`
#   仍然看到 2.2.6 → 崩。**所以插路径必须挪到任何第三方 import 之前。**
#
# 本板系统 numpy 是 2.2.6（sherpa_onnx 在用，**不能动**）；
# npu_libs 里的 1.26 只在 NPU 这条链路里**遮蔽**它，进程退出即失效。
# ══════════════════════════════════════════════════════════════════════
_HERE = os.path.dirname(os.path.abspath(__file__))
_TBE_LIBS = os.path.join(_HERE, "npu_libs")
if os.path.isdir(_TBE_LIBS):
    if _TBE_LIBS in sys.path:
        sys.path.remove(_TBE_LIBS)
    sys.path.insert(0, _TBE_LIBS)

import numpy as np                                # noqa: E402

# ── 依赖探测（失败不抛异常，让调用方回退 CPU 后端）──────────────
_TTS_DIR = os.environ.get("CAR_NPU_TTS_DIR",
                          os.path.expanduser("~/models/tts-npu"))
_HAS_ACL = False
_ACL_ERR = ""
_ACL_FIXED = False


def _ascend_lib_dirs():
    """昇腾运行库可能在的目录（按优先级）。"""
    home = os.environ.get("ASCEND_TOOLKIT_HOME") or \
        "/usr/local/Ascend/ascend-toolkit/latest"
    return [
        os.path.join(home, "lib64"),
        os.path.join(home, "runtime", "lib64"),
        "/usr/local/Ascend/driver/lib64",
        "/usr/local/Ascend/driver/lib64/common",
        "/usr/local/Ascend/driver/lib64/driver",
        "/var/davinci/driver/lib64",
    ]


def _try_import_acl():
    """导入 pyACL。**会兜底补 LD_LIBRARY_PATH**。

    ⚠️ 坑：`import acl` 需要 `libascendcl.so`，而它只在 set_env.sh 设的
    `LD_LIBRARY_PATH` 里。**用 `sudo -u <user>` 启动时 sudo 会重置环境**，
    或在别的会话里没 source set_env.sh，就会报
        ImportError: libascendcl.so: cannot open shared object file
    这里在导入失败时**主动把昇腾库目录加进 `LD_LIBRARY_PATH` 再试一次** ——
    比要求每个调用方都记得 source 更可靠。
    """
    global _ACL_ERR, _ACL_FIXED
    try:
        import acl                                  # noqa: F401
        return True, ""
    except Exception as e:
        err = repr(e)
    if "libascendcl" not in err and "libascend" not in err:
        return False, err

    cur = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [p for p in cur.split(":") if p]
    added = []
    for d in _ascend_lib_dirs():
        if os.path.isdir(d) and d not in parts:
            parts.append(d)
            added.append(d)
    if not added:
        return False, err
    os.environ["LD_LIBRARY_PATH"] = ":".join(parts)
    # ★★ 关键：**必须清掉失败缓存的 sys.modules 项**。
    #    第一次 `import acl` 失败时，Python 会把它记进 sys.modules，
    #    之后再 `import acl` 会直接复用那条失败记录，**根本不会重新 dlopen**。
    import sys as _sys
    for k in list(_sys.modules):
        if k == "acl" or k.startswith("acl."):
            del _sys.modules[k]
    # ★★ 真正管用的是**按依赖顺序预加载**（见该函数 docstring：
    #    单设 LD_LIBRARY_PATH 对已启动进程无效，直接 CDLL 主库也会因
    #    缺少兄弟库而失败）
    _preload_ascend_libs()
    try:
        import acl                                  # noqa: F401
        _ACL_FIXED = True
        return True, ""
    except Exception as e:
        return False, repr(e)


def _preload_ascend_libs():
    """按依赖顺序**预加载**昇腾运行库（`RTLD_GLOBAL`）。

    ⚠️ **为什么必须是"预加载"而不是设 `LD_LIBRARY_PATH`**：
    动态链接器（ld.so）**只在进程启动时读一次 `LD_LIBRARY_PATH`**，
    在 Python 里 `os.environ[...] = ...` 之后 `import acl` **无效** ——
    实测即使把路径设对，`import acl` 仍然报
    `ImportError: libascendcl.so: cannot open shared object file`。
    而且直接 `CDLL("libascendcl.so")` 也会失败，因为它自己还依赖
    `libascend_dump.so` 等一串兄弟库（报 `cannot open shared object file` 指向那个兄弟）。

    → 正确做法：用 `ldd` 拿到**全部依赖**，按**依赖顺序**（`ldd` 输出天然是拓扑序）
      逐个 `CDLL(..., RTLD_GLOBAL)`，把符号先灌进全局命名空间，
      之后 `import acl` 就能解析到 `libascendcl.so`。

    返回 (成功数, 错误列表)。
    """
    if not shutil.which("ldd"):
        return 0, ["没有 ldd"]
    libs = []
    for d in _ascend_lib_dirs():
        main = os.path.join(d, "libascendcl.so")
        if not os.path.isfile(main):
            continue
        try:
            out = subprocess.run(["ldd", main], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT,
                                 timeout=20).stdout.decode("utf-8", "replace")
        except Exception as e:
            return 0, ["ldd 失败：%r" % (e,)]
        for line in out.splitlines():
            line = line.strip()
            if "=>" not in line:
                continue
            name, _, path = line.partition("=>")
            path = path.strip().split(" ")[0]
            if not path or path == "not" or not os.path.isfile(path):
                continue
            if path not in libs:
                libs.append(path)
        break

    import ctypes
    n, errs = 0, []
    for p in libs:
        try:
            ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
            n += 1
        except Exception as e:
            errs.append("%s: %r" % (os.path.basename(p), e))
    return n, errs


_HAS_ACL, _ACL_ERR = _try_import_acl()


def ensure_runtime():
    """确保 pyACL 可用；必要时**原地重启进程**（`os.execve`）。

    ⚠️ **为什么需要这个**：动态链接器只在**进程启动时**读一次 `LD_LIBRARY_PATH`，
    所以在 Python 里改 `os.environ` 后再 `import acl` 是**无效**的
    （实测：路径设对了仍报 `libascendcl.so: cannot open shared object file`）。
    逐库预加载也没用 —— 每个昇腾库自己还依赖一串兄弟库
    （`libascend_dump.so` 缺 `libruntime.so`…），`dlopen` 解析不到。

    → 真正可靠的办法是**带着正确的环境重启自己**（`execve` 保留 PID、不 fork）。
    用环境变量 `_CAR_NPU_TTS_REEXEC` 做一次性标记，**绝不会无限重入**。

    返回 True = 已可用；False = 需要重启（调用方应立即 return，让 execve 生效）
    或确实不可用。
    """
    global _HAS_ACL, _ACL_ERR, _ACL_FIXED
    if _HAS_ACL:
        return True
    if os.environ.get("_CAR_NPU_TTS_REEXEC") == "1":
        return False                      # 重启过还是不行 → 真不可用

    cur = os.environ.get("LD_LIBRARY_PATH", "")
    parts = [p for p in cur.split(":") if p]
    for d in _ascend_lib_dirs():
        if os.path.isdir(d) and d not in parts:
            parts.append(d)
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(parts)
    env["_CAR_NPU_TTS_REEXEC"] = "1"
    env.setdefault("ASCEND_TOOLKIT_HOME",
                   "/usr/local/Ascend/ascend-toolkit/latest")
    try:
        os.execve(sys.executable, [sys.executable] + sys.argv, env)
    except Exception as e:
        _ACL_ERR = "execve 重启失败：%r（原错误：%s）" % (e, _ACL_ERR)
    return False                          # execve 成功则不会走到这里

def _ensure_tbe_libs_on_path():
    """把工程自带的 tbe 依赖目录（`npu_libs/`）加到 `sys.path` **最前**。

    ⚠️ 为什么必须自己做、还要放最前：
    官方 `synthesize.py` → `utils.py` → `import torch` / `scipy` 这条链
    需要 CANN tbe 的一批依赖；而 tbe 又要求 **numpy < 2**（它用了
    `np.float_`，NumPy 2.0 已移除），可本板系统装的是 numpy 2.2.6
    （`sherpa_onnx` 在用）。
    → 把一套 numpy 1.26 + scipy 等放在 `car_gui/npu_libs/`，
      **插到 sys.path 最前面**，让 NPU 这条链路拿到 numpy 1.26，
      而系统的 numpy 2.x 不受影响（只在本进程内遮蔽）。
    """
    lib = os.path.join(_HERE, "npu_libs")
    if os.path.isdir(lib):
        if lib in sys.path:
            sys.path.remove(lib)
        sys.path.insert(0, lib)
    return lib


def _install_scipy_stub():
    """用**标准库**顶掉官方代码里唯一的 scipy 依赖（`scipy.io.wavfile`）。

    ⚠️ **为什么必须这么做**（几条路都试过，全不通）：
    官方 `synthesize.py` 只有一行用到 scipy：
        `from scipy.io import wavfile` → `wavfile.write(path, rate, wav)`
    但装真 scipy 会引出**无法调和的 numpy 版本冲突**：
      · CANN 7.0.RC1 的 tbe 要求 **numpy < 2**（用了已移除的 `np.float_`）；
      · 本板系统是 **numpy 2.2.6**，且 `sherpa_onnx` 依赖它，**不能降级**；
      · npu_libs 里的 numpy 1.26 只在"**没有任何模块先 import numpy**"时
        才生效 —— 实际链路里 `tts_engine.py` 早就 import 过 numpy 了，
        于是 scipy（用 NumPy 1.x 编译）一加载就报
        `_ARRAY_API not found` / `numpy.core.multiarray failed to import`。
      （试过 `.pth` 前置、`sys.path.insert(0)` —— **都压不过已加载的 numpy**。）

    → 最干净的办法是**根本不需要 scipy**：读写 wav 用标准库 `wave` 即可。
    这里造一个 `scipy.io.wavfile` 替身塞进 `sys.modules`，
    官方代码**一行不改**照样跑。
    """
    import types
    import wave as _wave
    if "scipy.io.wavfile" in sys.modules:
        return False

    def write(filename, rate, data):
        """对齐 scipy.io.wavfile.write：[-1,1] 浮点或 int16 → 16bit 单声道 wav。"""
        a = np.asarray(data)
        if a.dtype.kind == "f":
            a = np.clip(a, -1.0, 1.0)
            pcm = (a * 32767.0).astype(np.int16)
        else:
            pcm = a.astype(np.int16)
        if pcm.ndim > 1:
            pcm = pcm.reshape(-1)
        with _wave.open(str(filename), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(int(rate))
            f.writeframes(pcm.tobytes())

    def read(filename, mmap=False):
        with _wave.open(str(filename), "rb") as f:
            rate = f.getframerate()
            raw = f.readframes(f.getnframes())
        return rate, np.frombuffer(raw, dtype=np.int16)

    scipy = types.ModuleType("scipy")
    io = types.ModuleType("scipy.io")
    wavfile = types.ModuleType("scipy.io.wavfile")
    wavfile.write = write
    wavfile.read = read
    io.wavfile = wavfile
    scipy.io = io
    sys.modules.setdefault("scipy", scipy)
    sys.modules.setdefault("scipy.io", io)
    sys.modules.setdefault("scipy.io.wavfile", wavfile)
    return True


#: 5 个 om 的固定文件名（官方 atc_static.sh 的产物名）
OM_FILES = {
    "encoder": "encoder_bs1.om",
    "variance_adaptor": "variance_adaptor_bs1.om",
    "decoder": "decoder_bs1.om",
    "postnet": "postnet_bs1.om",
    "vocoder": "hifigan_bs1.om",
}

#: 官方 `atc_static.sh` 里的静态上限（改这两个值必须**重新转 om**）
MAX_SRC_LEN = 64      # 音素上限（README：不要超过 256）
#: 梅尔帧上限的**默认值**；真实值在 `load()` 时从 om 的实际 shape 读出来
#: （官方 `atc_static.sh` 的 `max_mel_len`：250/500/750/1000/1250/1500/1750/2000）
#:
#: ⚠️ 这个值就是**单次合成时长上限**（官方代码超了会**静默截断**，丢句子尾巴）：
#:    · 250 帧 ≈ 2.9 秒音频 ≈ **约 25 个汉字** ← 早期用的值
#:    · 500 帧 ≈ 5.8 秒音频 ≈ **约 50 个汉字** ← 2026-09-30 改为这个
#: 实测踩坑：`我还没连上小车呢，听不见你的指令哦`（21 字）需要 **283 帧**，
#: 250 帧下**丢了最后两个字**。
MAX_MEL_LEN = 500
SAMPLE_RATE = 22050   # baker 女声

#: 单句汉字上限（**软上限**，超了还有 `_split_for_frames()` 用帧数兜底）。
#:
#: 🔴 **14 是实测出来的硬边界，不要再放宽**（2026-10-09 用音频时长反推帧数实测）：
#:    帧数 ≈ **16.53 × 字数 + 22**（22050Hz，86.13 帧/秒），
#:    解 16.53·n + 22 = 250 得 n ≈ **13.8 ⇒ 约 14 字**。
#:    实测各长度（音频时长 ÷ 86.13）：
#:       2字 48帧 | 3字 70 | 4字 93 | 6字 122 | 9字 180 | 10字 196 | **13字 225**
#:       **16字/17字/18字 全部顶到 250 帧上限**（音频都是 2.90s，即被截断）
#:    ⇒ 16~17 字的句子是**真的超限、必须分段**，没有"被冤枉切掉"的余地。
#:      （曾把这里放宽到 18 想减少分段，实测分段数毫无变化 —— 因为
#:        `_split_for_frames()` 按帧数又把它们切回来了，**放宽是无效的**。）
#:
#: ⚠️ 唯一能减少分段的办法是提高 `max_mel_len`，但已实测不可行：
#:    **300 帧合成慢 3 倍（3.06s）、400/500 帧 decoder 直接 SIGSEGV**（见 §TTS-on-NPU）。
DEFAULT_SENT_LEN = 14

#: 传给 `tts_engine.split_sentences` 的**初次切句**长度上限（按字数）。
#:
#: 🔴 为什么它 **不等于** `DEFAULT_SENT_LEN`（2026-10-09 实测踩到）：
#:   `DEFAULT_SENT_LEN=14` 是**防截断**的上限，真正的把关是
#:   `_split_for_frames()`（按梅尔帧数递归切，实测 0 截断）。
#:   但 `split_sentences` 是**按字数/标点**切的粗切 —— 若把 14 传给它，
#:   任何超过 14 字的片段（哪怕只有 8 字但带逗号）都会被切碎：
#:     实测 `"嗨，我是你的小车"`（8 字）被切成 2 段 → 多一次 ~1.1s 停顿。
#:   ⇒ 初次切句放宽到 24，只负责"别让一句话太长"；
#:     真正的截断保护交给帧数兜底。
SENT_SPLIT_MAXLEN = 24

# ── 阿拉伯数字 → 中文数字（**本后端必需**）────────────────────────────
#: ⚠️ **FastSpeech2 读不出阿拉伯数字**（2026-09-30 用户实测报的 bug）：
#:   它的流程是「汉字 → 拼音 → 音素」，而**数字没有拼音映射**，
#:   官方 `utils.preprocess_mandarin` 会把它们静默变成 `sp`（静音）。
#:   实测预处理结果：
#:       "加速到313"       → {j ia1 s u4 d ao4 sp}            ← "313" 整段没了
#:       "313"             → {sp}                            ← 只剩静音
#:       "加速到三百一十三"  → {j ia1 s u4 d ao4 s an1 ...}     ← 正常
#:   表现：用户听到「加速到」就**戛然而止**（旧 Kokoro 能读数字，
#:   换成 NPU 后端后这个差异才暴露）。
#: ⇒ 在本层统一把数字转成中文，**所有走 NPU 的播报都自动受益**
#:   （包括 LLM 生成的"往前开500毫米"这类）。
_CN_DIGITS = "零一二三四五六七八九"


def num_to_cn(n):
    """整数 → 中文读法（0~9999）。313→"三百一十三"、250→"二百五十"、15→"十五"。"""
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return str(n)
    if n < 0:
        return "负" + num_to_cn(-n)
    if n == 0:
        return "零"
    if n > 9999:
        return str(n)
    units = ["", "十", "百", "千"]
    s, pos = str(n), len(str(n))
    parts = []
    for i, ch in enumerate(s):
        d, u = int(ch), units[pos - 1 - i]
        if d == 0:
            parts.append("")
        elif d == 1 and u == "十" and i == 0:
            parts.append("十")
        else:
            parts.append(_CN_DIGITS[d] + u)
    out = ""
    for i, p in enumerate(parts):
        if p:
            if i > 0 and not parts[i - 1] and out and not out.endswith("零"):
                out += "零"
            out += p
    return out


def normalize_text(text):
    """播报前的文本规范化（NPU 后端专用）。

    两件事：
      ① **阿拉伯数字 → 中文**（本后端读不出数字，见 `_CN_DIGITS`）；
      ② **单位符号 → 中文读法**：`mm/s` → "毫米每秒"。
         ⚠️ 为什么连单位也要转（2026-09-30 用户反馈）：
         "mm/s" 里的 `/` 同样没有拼音映射，会被吃掉；而且就算留下 "mm"
         也读不出来。所以「速度350」不补单位就听着像话没说完，
         补成「速度三百五十毫米每秒」才完整。
         顺带把常见符号一起处理（`%`/`°`/`km/h`），避免同类问题。
    """
    if not text:
        return text
    import re as _re
    s = str(text)
    # ① 单位/符号（**必须在数字转换之前**，否则 "mm/s" 里的 s 会被误处理）
    s = _re.sub(r"mm\s*/\s*s\b", "毫米每秒", s, flags=_re.I)
    s = _re.sub(r"m\s*/\s*s\b", "米每秒", s, flags=_re.I)
    s = _re.sub(r"km\s*/\s*h\b", "公里每小时", s, flags=_re.I)
    s = s.replace("mm", "毫米").replace("cm", "厘米").replace("kg", "公斤")
    # ② 百分号要**前置**："12%" → "百分之十二"（不是"十二百分之"）
    s = _re.sub(r"(\d+(?:\.\d+)?)\s*%",
                lambda m: "百分之" + num_to_cn(m.group(1)), s)
    # ③ 小数（必须先于整数处理，否则 "1.5" 会被拆成 "一" + ".五"）
    s = _re.sub(r"(\d+)\.(\d+)",
                lambda m: num_to_cn(m.group(1)) + "点"
                + "".join(_CN_DIGITS[int(c)] for c in m.group(2)), s)
    # ④ 阿拉伯数字 → 中文
    s = _re.sub(r"\d+", lambda m: num_to_cn(m.group()), s)
    # ⑤ 单位替换可能留下空格（"300 mm/s" → "三百 毫米每秒"），读起来会有停顿
    s = _re.sub(r"\s+", "", s)
    return s


def available():
    """NPU 后端现在可用吗？返回 (是否可用, 原因)。"""
    global _HAS_ACL, _ACL_ERR
    if not _HAS_ACL:
        # 试一次"带环境重启自己"；若 execve 生效，本行之后的代码不再执行，
        # 重启后的进程会在 import 阶段就拿到可用的 acl。
        if ensure_runtime():
            pass
        else:
            return False, (
                "pyACL 不可用：%s\n"
                "  提示：可先 `source /usr/local/Ascend/ascend-toolkit/set_env.sh`，"
                "或让启动脚本保证 LD_LIBRARY_PATH 含昇腾库目录。" % _ACL_ERR)
    if not os.path.isdir(_TTS_DIR):
        return False, "模型目录不存在：%s" % _TTS_DIR
    missing = [f for f in OM_FILES.values()
               if not os.path.isfile(os.path.join(_TTS_DIR, f))]
    if missing:
        return False, "缺少 om：%s" % ", ".join(missing)
    if not os.path.isfile(os.path.join(_TTS_DIR, "preprocess.yaml")):
        return False, "缺少 preprocess.yaml"
    return True, "就绪（%s）" % _TTS_DIR


class NpuTtsEngine(object):
    """与 `TtsEngine` 同接口的 NPU 后端。"""

    #: 后端标识（GUI 用它显示"本地/云端"那种标签）
    backend = "npu"

    def __init__(self, log=None, tts_dir=None):
        self._log = log or (lambda s: None)
        self._dir = tts_dir or _TTS_DIR
        self._model = None
        self.sample_rate = SAMPLE_RATE
        #: 从 om 实际 shape 读出（load() 时填）；决定单次合成时长上限
        self.max_src_len = MAX_SRC_LEN
        self.max_mel_len = MAX_MEL_LEN
        self._sinks = []
        self._play_dev = None
        # 合成**全局串行**：NPU 只有一个 AI core，并发只会互相抢
        self._gen_lock = threading.Lock()
        self._stats = {"calls": 0, "total": 0.0, "last": 0.0,
                       "truncated": 0, "errors": 0}
        # 复用 tts_engine 的播放/分句（不重复造轮子）
        self._tts_engine_mod = None

    # ── 内部：懒加载 tts_engine（为了复用它的播放与分句逻辑）────
    @property
    def _tmod(self):
        if self._tts_engine_mod is None:
            import tts_engine as _t
            self._tts_engine_mod = _t
        return self._tts_engine_mod

    @property
    def ready(self):
        return self._model is not None

    @property
    def num_speakers(self):
        return 1          # baker 单说话人（官方模型 n_speakers=1）

    def status(self):
        return {
            "backend": "npu",
            "ready": self.ready,
            "dir": self._dir,
            "sample_rate": self.sample_rate,
            "num_speakers": self.num_speakers,
            "max_src_len": self.max_src_len,
            "max_mel_len": self.max_mel_len,
            "stats": dict(self._stats),
        }

    # ── 加载 ────────────────────────────────────────────────────────
    def load(self, tts_dir=None, num_threads=2, provider="cpu",
             model_type="auto", lang=None):
        """加载 5 个 om。返回 (是否成功, 提示)。签名与 `TtsEngine.load` 一致。"""
        ok, why = available()
        if not ok:
            return False, why

        d = tts_dir or self._dir
        # 官方 synthesize.py 用相对路径找 config/lexicon，必须切到模型目录
        old_cwd = os.getcwd()
        t0 = time.time()
        try:
            os.chdir(d)
            # ★ 让 `import synthesize` 找得到：npu_tts/ 放官方代码，
            #   而它内部 `from utils import ...` 需要 npu_tts 在 sys.path
            npu_pkg = os.path.join(_HERE, "npu_tts")
            for p in (_HERE, npu_pkg):
                if p not in sys.path:
                    sys.path.insert(0, p)
            # ★ 切断 scipy/numpy 版本冲突（官方只用 scipy 写 wav，标准库足够）
            _install_scipy_stub()
            from synthesize import FastSpeech2Model          # noqa: E402
            models = {k: os.path.join(d, v)
                      for k, v in OM_FILES.items() if k != "vocoder"}
            self._model = FastSpeech2Model(
                os.path.join(d, "preprocess.yaml"), models,
                os.path.join(d, OM_FILES["vocoder"]), device_id=0)

            # ★★ 从 om 的**实际 shape** 读回真实上限（别硬编码）：
            #    官方 FastSpeech2Model 已经把 encoder/decoder 的首个输入
            #    shape[1] 读成了 max_text_len / max_mel_len，
            #    这两个值决定"单次能合成多长"。
            try:
                self.max_src_len = int(self._model.max_text_len)
                self.max_mel_len = int(self._model.max_mel_len)
            except Exception:
                pass
        except Exception as e:
            self._model = None
            return False, "加载 NPU TTS 失败：%r" % (e,)
        finally:
            os.chdir(old_cwd)

        self._dir = d
        self._log("NPU TTS：模型加载完成 %.2fs（%s；音素上限 %d，梅尔帧上限 %d"
                  "≈%.1f 秒音频）" % (
                      time.time() - t0, d, self.max_src_len, self.max_mel_len,
                      self.max_mel_len / 86.13))
        return True, ("NPU TTS 就绪（FastSpeech2, 22050Hz, %.2f 秒加载；"
                      "单次上限 %d 帧≈%.1f 秒）") % (
            time.time() - t0, self.max_mel_len, self.max_mel_len / 86.13)

    # ── 合成 ────────────────────────────────────────────────────────
    def synth(self, text, speaker=0, speed=1.0):
        """
        合成一句话，返回 `(采样率, float32 数组)`；失败返回 `(0, None)`。

        与 `TtsEngine.synth` 同签名。`speaker` 被忽略（单说话人）；
        `speed` 通过官方 `duration_control` 实现（>1 变慢，<1 变快）。
        """
        text = (text or "").strip()
        if not self.ready or not text:
            return 0, None
        if np is None:
            return 0, None
        # ★ 阿拉伯数字必须转中文，否则会被官方前处理静默变成静音（见 _CN_DIGITS）
        text = normalize_text(text)

        with self._gen_lock:
            t0 = time.time()
            try:
                # duration_control 范围 (0,2)，默认 1.0：值越大语速越慢
                dc = min(max(float(speed), 0.5), 2.0)
                # ★★ 捕获官方代码的 stdout 以便**检测静默截断**。
                #    官方在超限时只 print 一句 "Truncate the input."，
                #    **不抛异常、不改返回值** → 表现成"最后几个字没读出来"，
                #    极难定位（本问题就是这么来的）。这里把它抓出来记进日志。
                import contextlib
                import io as _io
                _buf = _io.StringIO()
                with contextlib.redirect_stdout(_buf):
                    self._model.synthesize(
                        text, [1.0, 1.0, dc], output_dir=self._out_dir())
                _out = _buf.getvalue()
            except Exception as e:
                self._stats["errors"] += 1
                self._log("NPU TTS 合成失败：%s" % e)
                return 0, None

            # 从官方输出里解析真实需要的梅尔帧数，并判断是否被截断
            import re as _re
            m = _re.search(r"Current mel length:\s*\[(\d+)\]", _out)
            need = int(m.group(1)) if m else 0
            if need > self.max_mel_len:
                self._stats["truncated"] = self._stats.get("truncated", 0) + 1
                self._log("⚠ NPU TTS 截断：「%s」需要 %d 帧 > 上限 %d 帧，"
                          "**末尾会被丢掉**。请拆成更短的句子"
                          "（当前上限约 %.1f 秒音频）"
                          % (text[:20], need, self.max_mel_len,
                             self.max_mel_len / 86.13))

            dt = time.time() - t0
            self._stats["calls"] += 1
            self._stats["last"] = dt
            self._stats["total"] += dt

            # 官方 synthesize.py 把结果写成 wav（`_write_wav`），
            # 这里读回来转 float32 —— 比改官方代码更安全（它一行没动）。
            try:
                import wave
                w = self._last_wav()
                if not w:
                    self._stats["errors"] += 1
                    return 0, None
                with wave.open(w, "rb") as f:
                    sr = f.getframerate()
                    n = f.getnframes()
                    raw = f.readframes(n)
                pcm = np.frombuffer(raw, dtype=np.int16)
                if pcm.size == 0:
                    self._stats["errors"] += 1
                    return 0, None
                data = pcm.astype(np.float32) / 32768.0
                return sr, data
            except Exception as e:
                self._stats["errors"] += 1
                self._log("NPU TTS 读回音频失败：%s" % e)
                return 0, None

    #: 帧数估算的斜率和截距 —— **按实测音频时长反推拟合**（见 `_est_frames`）
    _EST_SLOPE = 16.53
    _EST_INTERCEPT = 22

    def _est_frames(self, text):
        """粗估一段文本要多少梅尔帧（用来判断"要不要再切"）。

        实测方法：合成后**量音频时长 × 86.13** 反推帧数（官方那句
        `Current mel length` 是 C 层直接写 fd 1，Python 侧抓不到）。
        实测数据（22050Hz）：
            2字 48帧 | 3字 70 | 4字 93 | 6字 122 | 9字 180 | 10字 196 | 13字 225
            16字/17字/18字 全部 250 帧（顶到上限）
        线性拟合约 **16.53 帧/字 + 22**（固定开销约 22 帧）。

        ⚠️ 别再"取偏保守的更大系数"：那会让**本来放得下的句子**被
        `_split_for_frames()` 多切一刀（多一次 ~1.07s 停顿）。
        兜底靠的是 `_split_for_frames()` 的递归切分，不是把估算放大。
        """
        return int(len(text) * self._EST_SLOPE) + self._EST_INTERCEPT

    def _split_for_frames(self, sents):
        """把"估算仍超上限"的句子**递归对半切**，保证不会静默截断。"""
        limit = max(1, int(self.max_mel_len))
        out, stack = [], list(sents)
        while stack:
            s = stack.pop(0)
            if self._est_frames(s) <= limit or len(s) <= 1:
                out.append(s)
                continue
            # 优先在标点处切，其次对半切
            import re as _re
            marks = [m.start() for m in _re.finditer(r"[。！？!?；;，,、]", s)]
            cut = None
            if marks:
                mid = len(s) / 2.0
                cut = min(marks, key=lambda i: abs((i + 1) - mid)) + 1
            if not cut or cut <= 0 or cut >= len(s):
                cut = max(1, len(s) // 2)
            head, tail = s[:cut].strip(), s[cut:].strip()
            for piece in (tail, head):          # 栈：先切后半再前半（保持顺序）
                if piece:
                    stack.insert(0, piece)
        return out

    def _out_dir(self):
        d = os.path.join(self._dir, "output")
        os.makedirs(d, exist_ok=True)
        return d

    def _last_wav(self):
        """取官方刚写出的 wav（按修改时间最新）。"""
        d = self._out_dir()
        try:
            fs = [os.path.join(d, f) for f in os.listdir(d)
                  if f.endswith(".wav")]
        except Exception:
            return None
        if not fs:
            return None
        return max(fs, key=os.path.getmtime)

    # ── 播放（复用 TtsEngine 的实现，含 sink 机制）────────────────
    def add_sink(self, fn):
        if fn not in self._sinks:
            self._sinks.append(fn)
        return fn

    def remove_sink(self, fn):
        try:
            self._sinks.remove(fn)
            return True
        except ValueError:
            return False

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

    def play_samples(self, sr, data, device=None, sink=None):
        """
        播放一段内存里的音频。返回 `(是否成功, 提示)`。

        这里**不委托** `TtsEngine.play_samples`（那是给 TtsEngine 实例用的方法），
        而是复用它的**静态** `_run` 与模块级 `output_probe`，逻辑保持一致：
        先显式设备（并记住成功过的），再 PulseAudio，最后才试默认设备。
        """
        import tempfile
        T = self._tmod
        if data is None or len(data) == 0:
            return False, "没有音频数据"
        probe = T.output_probe()
        if not probe["ok"]:
            return False, T.NO_OUTPUT_HINT
        if not probe["players"]:
            return False, "系统里没有 paplay / aplay / play，无法播放"

        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="tts_npu_")
        os.close(fd)
        # ★ 记下"播放世代"：用户在播放中途按 F2 会递增它（见
        #   tts_engine._PLAY_EPOCH）。之后每次重试前对一次 ——
        #   世代变了说明**用户要打断**，绝不能换设备再播一遍。
        epoch = T.playback_epoch()

        def _cancelled():
            return T.playback_epoch() != epoch

        try:
            self._write_wav(tmp, sr, data)
            if sink:
                ok, msg = T.TtsEngine._run(["paplay", "-d", sink, tmp])
                return (True, "已播放（paplay → %s）" % sink) if ok else (False, msg)
            if device:
                ok, msg = T.TtsEngine._run(["aplay", "-D", device, tmp])
                return (True, "已播放（aplay → %s）" % device) if ok else (False, msg)

            tried = []
            if self._play_dev:                      # ① 上次成功的设备
                ok, msg = T.TtsEngine._run(["aplay", "-D", self._play_dev, tmp])
                if ok:
                    return True, "已播放（aplay -D %s）" % self._play_dev
                tried.append("aplay -D %s: %s" % (self._play_dev, msg))
                self._play_dev = None
                if _cancelled():
                    return False, "已被打断（放弃重试）"
            if shutil.which("paplay") and probe["sinks"]:   # ② PulseAudio
                if _cancelled():
                    return False, "已被打断（放弃重试）"
                ok, msg = T.TtsEngine._run(["paplay", tmp])
                if ok:
                    return True, "已播放（paplay，默认 sink）"
                tried.append("paplay: %s" % msg)
            if shutil.which("aplay"):               # ③ 显式 ALSA 设备
                for d in probe["alsa"]:
                    if _cancelled():
                        return False, "已被打断（放弃重试）"
                    ok, msg = T.TtsEngine._run(["aplay", "-D", d["id"], tmp])
                    if ok:
                        self._play_dev = d["id"]
                        return True, "已播放（aplay -D %s / %s）" % (
                            d["id"], d.get("name", ""))
                    tried.append("aplay -D %s: %s" % (d["id"], msg))
                if _cancelled():
                    return False, "已被打断（放弃重试）"
                ok, msg = T.TtsEngine._run(["aplay", tmp])   # ④ 默认设备
                if ok:
                    return True, "已播放（aplay 默认设备）"
                tried.append("aplay: %s" % msg)
            return False, "播放失败：%s" % "；".join(tried)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def say(self, text, speaker=0, speed=1.0, device=None, sink=None,
            should_stop=None):
        """
        分句合成 + 边合成边播。返回 `(是否成功, 提示)`。

        ⚠️ **分句上限比 sherpa 后端更严**：NPU 模型 `max_src_len=64` 音素
        （≈25~30 汉字），超了会被官方代码**静默截断**（丢字）。
        所以这里用 `DEFAULT_SENT_LEN` 主动分句。
        """
        T = self._tmod
        # ★ 这里传 `SENT_SPLIT_MAXLEN`（24）而**不是** `DEFAULT_SENT_LEN`（14）：
        #   14 是"**防截断**"的字数上限，由下面 `_split_for_frames()` 按**帧数**
        #   精确兜底；若用它去调 `split_sentences`，会因为"一超 14 就切"
        #   而把 `"嗨，我是你的小车"`（8 字）也切成 2 段 → 白白多一次 1.1s 停顿。
        #   （`split_sentences` 现在也只在"片段超过 max_len"时才用逗号切，
        #     两者配合就不会把短句切碎。）
        sents = T.split_sentences(text, max_len=SENT_SPLIT_MAXLEN)
        # ★★ 自适应再切：`DEFAULT_SENT_LEN` 只是**按字数**的粗估，而截断是按
        #    **梅尔帧数**判的，两者并不等比（实测有的句子 ~19 帧/字，
        #    14 字的片段仍需 268 帧 > 250 上限）。所以对"估算仍可能超限"的
        #    片段再对半切，直到放得下 —— 这样**任何长度都不会再被静默截断**。
        sents = self._split_for_frames(sents)
        if not sents:
            return False, "文本为空"

        probe = T.output_probe()
        if not probe["ok"] and not self._sinks:
            return False, T.NO_OUTPUT_HINT

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
            #   本板每句合成固定 ~1.11s，这期间用户很可能已经按下 F2。
            #   此时这一句**绝不能播出去** —— 否则麦克风刚要打开又被播报占住
            #   （播放/采集硬件互斥），用户看到的就是"录音启动失败"。
            #   有了这道检查，上层就能**只等播放停止、不等合成结束**
            #   （见 `CarController.yield_speaker`），
            #   按 F2 到麦克风可用的等待从 ~1.1s+ 降到 ~0.1s。
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
                    errs.append("第 %d 句播放失败：%s" % (
                        i + 1, (msg or "").splitlines()[0][:130]))
            if delivered:
                done += 1

        if done == len(sents):
            how = "本地播放" if probe["ok"] else "已送到 %d 个输出端" % len(self._sinks)
            return True, "已播报 %d 句（%s，NPU）" % (done, how)
        if done:
            return True, "已播报 %d/%d 句；%s" % (done, len(sents), "；".join(errs))
        return False, "；".join(errs) or "播报失败"

    def close(self):
        """释放 5 个 om（引用计数在 acl_infer_session 里管）。"""
        m = self._model
        self._model = None
        if m is None:
            return
        for name in ("fastspeech2", "vocoder"):
            obj = getattr(m, name, None)
            if obj is None:
                continue
            seq = obj if isinstance(obj, (list, tuple)) else [obj]
            for s in seq:
                try:
                    s.free_resource()
                except Exception:
                    pass


# ══════════════════════════════════════════════════════════════════════
#  CLI 自测
# ══════════════════════════════════════════════════════════════════════

def _main():
    import argparse
    ap = argparse.ArgumentParser(description="NPU TTS（FastSpeech2 on Ascend）")
    ap.add_argument("--text", default="大家好", help="要合成的文本")
    ap.add_argument("--save", default=None, help="存成 wav 的路径")
    ap.add_argument("--bench", action="store_true", help="跑多句测 RTF")
    ap.add_argument("--status", action="store_true", help="只看可用性")
    a = ap.parse_args()

    ok, why = available()
    print("可用性: %s —— %s" % (ok, why))
    if a.status or not ok:
        return 0 if ok else 1

    eng = NpuTtsEngine(log=lambda m: print("  " + m))
    ok, msg = eng.load()
    print("加载  : %s —— %s" % (ok, msg))
    if not ok:
        return 1
    print("状态  :", eng.status())
    print()

    if a.bench:
        cases = ["大家好", "已急停", "你好，我是你的智能小车",
                 "检测到障碍物，请小心"]
        import wave
        rows = []
        for t in cases:
            t0 = time.time()
            sr, data = eng.synth(t)
            dt = time.time() - t0
            if data is None:
                print("  ❌ %-14s 合成失败" % t)
                continue
            dur = len(data) / float(sr)
            rtf = dt / dur if dur else float("inf")
            rows.append((t, dt, dur, rtf))
            print("  %-16s 合成 %5.2fs  音频 %5.2fs  RTF %5.2f"
                  % (t, dt, dur, rtf))
        if rows:
            ar = sum(r[3] for r in rows) / len(rows)
            print()
            print("  平均合成 %.2fs   平均 RTF %.2f"
                  % (sum(r[1] for r in rows) / len(rows), ar))
            print("  ★ 对照 Kokoro(CPU) RTF 5.43 → 加速 %.2fx" % (5.43 / ar))
        eng.close()
        return 0

    sr, data = eng.synth(a.text)
    if data is None:
        print("合成失败")
        eng.close()
        return 1
    print("合成: %d Hz, %d 采样点, %.2f 秒" % (sr, len(data), len(data) / sr))
    if a.save:
        eng._write_wav(a.save, sr, data)
        print("已保存:", a.save)
    eng.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
