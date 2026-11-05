# -*- coding: utf-8 -*-
"""
gen_wake_prompts.py —— 生成「语音唤醒」用的**预录提示音**（16bit 单声道 WAV）

为什么要预录而不是每次现合成
--------------------------
本板 TTS 的 NPU 后端 RTF≈0.9、CPU 后端 RTF≈5.4：唤醒后再现合成一句提示音
要等 0.5~2 秒才出声，用户会觉得"它没理我"。预录一个 wav 直接 aplay，几十毫秒出声。

产物（都放在本脚本所在目录）
--------------------------
    wake_zh.wav     NPU TTS 合成的「在听」        ← 首选（car_gui_core 默认用它）
    wake_zh2.wav    NPU TTS 合成的「等待指令」    ← 备选，改 CAR_WAKE_PROMPT 可用
    wake_beep.wav   标准库 wave 生成的短嘟声      ← **兜底，永远生成**

🔴 兜底为什么必须存在：NPU TTS 依赖 CANN/om 模型，板子重装或模型缺失时
   会失败。没有兜底就会变成"唤醒了却一声不吭"，用户以为坏了。
   本脚本的语义是：**先保证一定有声音，再尽量要人声。**

跑法（板上一律用 run_py.sh，绝不用裸 python3）
--------------------------------------------
    cd ~/car_gui
    bash run_py.sh wake_prompts/gen_wake_prompts.py
    bash run_py.sh wake_prompts/gen_wake_prompts.py --beep-only   # 只生成嘟声

自测（不需要 GUI）：跑完看 stdout 的汇总行即可。

⚠️ 这一条**不用来验唤醒词的 KWS 命中率**：本项目已实测「合成音送 KWS 不可用」
   （声学分布与真人差太远，WenetSpeech 训练的 KWS 认不出 TTS 合成音）。
   提示音只是给人听的；唤醒词能否被听到**必须用真人录音验证**。
"""

import math
import os
import struct
import sys
import time
import wave

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
# run_py.sh 会 cd 到 car_gui 再执行本脚本 → 脚本目录是 wake_prompts/，
# 必须把 car_gui 放进 sys.path 才能 import tts_engine。
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

OUT_DIR = _HERE
BEEP_WAV = os.path.join(OUT_DIR, "wake_beep.wav")
ZH_WAV = os.path.join(OUT_DIR, "wake_zh.wav")
ZH2_WAV = os.path.join(OUT_DIR, "wake_zh2.wav")

#: 提示音必须是**极短**的（2~4 字）。超过这个时长就裁剪/告警。
MAX_PROMPT_SEC = 1.6


def write_wav(path, rate, samples):
    """把 [-1,1] 的 float 列表写成 16bit 单声道 wav。"""
    pcm = struct.pack("<%dh" % len(samples),
                      *[max(-32768, min(32767, int(s * 32767))) for s in samples])
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(rate))
        f.writeframes(pcm)
    return path


def make_beep(path=BEEP_WAV, rate=22050):
    """生成 0.2s 双音"嘀-嘟"提示音（带淡入淡出，避免爆音）。"""
    def tone(freq, sec, amp=0.35, fade=0.01):
        n = int(rate * sec)
        nf = max(1, int(rate * fade))
        out = []
        for i in range(n):
            v = amp * math.sin(2 * math.pi * freq * i / rate)
            if i < nf:
                v *= i / float(nf)
            elif i > n - nf:
                v *= max(0.0, (n - i) / float(nf))
            out.append(v)
        return out

    samples = tone(880.0, 0.09) + [0.0] * int(rate * 0.04) + tone(1174.7, 0.13)
    write_wav(path, rate, samples)
    return path, len(samples) / float(rate)


def trim_silence(samples, rate, thr=0.01, pad_sec=0.03):
    """裁掉首尾静音（FastSpeech2 输出两端常有静音，会让开录变慢）。"""
    try:
        import numpy as np
        a = np.asarray(samples, dtype=np.float32)
    except Exception:
        return samples
    idx = np.where(np.abs(a) > thr)[0]
    if idx.size == 0:
        return samples
    pad = int(rate * pad_sec)
    lo = max(0, int(idx[0]) - pad)
    hi = min(len(a), int(idx[-1]) + pad)
    return a[lo:hi]


def gen_tts_wavs():
    """用 NPU TTS（不可用则自动回退 CPU 后端）合成提示音。

    返回 (是否成功, 说明)。
    """
    try:
        import tts_engine as T
    except Exception as e:
        return False, "import tts_engine 失败：%r" % (e,)

    eng = T.TtsEngine(log=lambda s: print("  [TTS] %s" % s))
    t0 = time.time()
    backend, ok, msg = eng.load_best(prefer_npu=True, num_threads=2)
    print("  TTS 加载：backend=%s ok=%s msg=%s（%.1fs）"
          % (backend, ok, msg, time.time() - t0))
    if not ok:
        return False, "TTS 加载失败：%s" % msg

    try:
        spk = T.default_speaker(eng._dir)
    except Exception:
        spk = 0
    print("  说话人：%s" % spk)

    done = []
    for text, path in (("在听", ZH_WAV), ("等待指令", ZH2_WAV)):
        t1 = time.time()
        sr, data = eng.synth(text, spk)
        if data is None or not len(data):
            print("  ⚠ 合成「%s」失败，跳过" % text)
            continue
        dur_raw = len(data) / float(sr)
        data = trim_silence(data, sr)
        dur = len(data) / float(sr)
        if dur > MAX_PROMPT_SEC:
            print("  ⚠ 合成「%s」有 %.2fs，偏长（提示音应 2~4 字）；"
                  "已照写，建议换更短的词" % (text, dur))
        eng._write_wav(path, sr, data)
        print("  ✓ %s ← 「%s」（%d Hz，裁静音 %.2fs→%.2fs，合成 %.2fs）"
              % (os.path.basename(path), text, sr, dur_raw, dur, time.time() - t1))
        done.append(path)
    return bool(done), "生成 %d 个提示音（backend=%s）" % (len(done), backend)


def main():
    beep_only = "--beep-only" in sys.argv
    print("生成目录：%s" % OUT_DIR)
    os.makedirs(OUT_DIR, exist_ok=True)

    path, dur = make_beep()
    print("✓ 兜底嘟声：%s（%.2fs，%d 字节）"
          % (os.path.basename(path), dur, os.path.getsize(path)))

    ok = True
    if beep_only:
        print("（--beep-only：跳过 TTS）")
    else:
        ok, msg = gen_tts_wavs()
        print("%s TTS 提示音：%s" % ("✓" if ok else "✗", msg))

    print("\n汇总：")
    for name in ("wake_zh.wav", "wake_zh2.wav", "wake_beep.wav"):
        p = os.path.join(OUT_DIR, name)
        if os.path.isfile(p):
            with wave.open(p, "rb") as f:
                print("  %-14s %6d 字节  %d Hz  %d ch  %d bit  %.2fs"
                      % (name, os.path.getsize(p), f.getframerate(),
                         f.getnchannels(), f.getsampwidth() * 8,
                         f.getnframes() / float(f.getframerate() or 1)))
        else:
            print("  %-14s （无）" % name)
    # 兜底嘟声已生成 → 即使 TTS 失败也算"可用"，返回 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
