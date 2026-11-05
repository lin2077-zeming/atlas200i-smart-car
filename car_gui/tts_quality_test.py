#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tts_quality_test.py —— TTS 音质**客观**测量：合成 → 用板载 ASR 反听 → 打分

为什么要做这个工具
------------------
"音质差"是个很主观的说法。凭感觉换模型会一直试错，而且换完还是说不清
"到底好没好"。这个方法把它变成**可比的数字**：

    合成一句话 → 用板上的 SenseVoice 把这段音频**再听一遍**
    → 看它听出来的字对不对，跟原文比字符准确率

**ASR 听得越准，说明音频越清楚、越像人话。** 这是最贴近"人能不能听懂"
的客观代理指标 —— 因为 SenseVoice 和真人听的是同一段声学信息。

同时量到的还有：

| 指标 | 说明 |
|---|---|
| 音频时长 / 合成耗时 / **RTF** | RTF = 合成秒数 ÷ 音频秒数，>1 就是"慢于实时" |
| 峰值 / RMS | 响度。峰值 <0.2 偏小，车上要放大 |
| 削顶占比 | 波形被削平的比例，>0.1% 会失真 |
| 语音段占比 | 有声音的帧占多少，太低说明大量静音或截断 |
| **ASR 反听文本 + 准确率** | ★ 核心指标 |

用法
----
    # 测当前自动选中的模型（自动挑音质最好的那个）
    bash run_py.sh tts_quality_test.py

    # 试几个音色（模型有多个说话人时）
    bash run_py.sh tts_quality_test.py --speakers 0,3,42,100

    # 指定模型目录（用来对比两个模型）
    bash run_py.sh tts_quality_test.py --dir ~/models/tts/kokoro-multi-lang-v1_0

    # 换自己的测试文本
    bash run_py.sh tts_quality_test.py --text "已急停" --text "前方有障碍"

    # 跳过 ASR（省时间；但那样就只有"量"没有"质"）
    bash run_py.sh tts_quality_test.py --no-asr

生成的 wav 落在 `--out-dir`（默认 /tmp/tts_quality/），可以 scp 回 PC 亲耳听。
"""

import argparse
import difflib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tts_engine as T                                    # noqa: E402

try:
    import numpy as np
except Exception:
    np = None

# pypinyin 用来做**同音容忍**比对。没有它也能跑，只是少一个指标。
try:
    from pypinyin import lazy_pinyin as _lazy_pinyin, Style as _PyStyle
    _HAS_PYPINYIN = True
except Exception:
    _HAS_PYPINYIN = False

# 用于评测的句子：**都是真实车载播报语**，不是随便找的句子
DEFAULT_TEXTS = [
    "已急停",
    "前方有障碍，已停止",
    "开始前进，请注意安全",
    "电量不足，请充电",
]

CLIP_THRESHOLD = 0.99


# ══════════════════════════════════════════════════════════════════════
#  客观指标
# ══════════════════════════════════════════════════════════════════════
def analyse(data, rate, frame_ms=20):
    """
    算一段音频的客观指标。返回 dict。

    语音段占比用能量端点检测：底噪取全部帧 RMS 的 20% 分位数，
    超过底噪 4 倍算"在说话"（与 kws_sample_check.py 同一套判据）。
    """
    n = len(data)
    dur = n / float(rate)
    peak = float(np.max(np.abs(data))) if n else 0.0
    rms = float(np.sqrt(np.mean(data * data) + 1e-12)) if n else 0.0
    clip = float(np.mean(np.abs(data) > CLIP_THRESHOLD)) if n else 0.0

    m = max(1, int(rate * frame_ms / 1000.0))
    nfr = n // m
    if nfr >= 2:
        fr = np.sqrt(np.mean(data[:nfr * m].reshape(nfr, m) ** 2, axis=1) + 1e-12)
        noise = float(np.percentile(fr, 20))
        thr = max(noise * 4.0, 0.004)
        voiced = fr > thr
        ratio = float(np.mean(voiced))
        idx = np.where(voiced)[0]
        span = ((idx[-1] - idx[0] + 1) * m / float(rate)) if len(idx) else 0.0
    else:
        noise, ratio, span = 0.0, 0.0, 0.0

    return {"dur": dur, "peak": peak, "rms": rms, "clip": clip,
            "speech_ratio": ratio, "speech_span": span, "noise": noise}


def text_accuracy(ref, hyp):
    """字符级相似度（0~1）。**严格**：同音字（已/一、急/级）会被判错。"""
    if not ref:
        return 0.0
    return difflib.SequenceMatcher(None, ref, hyp or "").ratio()


def pinyin_seq(text, with_tone=False):
    """把中文转成拼音序列；没有 pypinyin 时返回 None。"""
    if not _HAS_PYPINYIN or not text:
        return None
    try:
        sty = _PyStyle.TONE3 if with_tone else _PyStyle.NORMAL
        return _lazy_pinyin(text, style=sty, errors="ignore")
    except Exception:
        return None


def pinyin_accuracy(ref, hyp, with_tone=False):
    """
    拼音级相似度（0~1）。

    **为什么需要它**：ASR 对合成音经常"音听对了、字选错了"——
    比如把「已急停」写成「一级停」。字符比对会把这判成 2/3 错，
    但实际上发音是准的、人完全听得懂。

    所以看两个指标：
      · 字符准确率  低 + 拼音准确率高  → **音频没问题**，只是同音字
      · 两个都低                      → 音频真的糊了
    """
    a = pinyin_seq(ref, with_tone)
    b = pinyin_seq(hyp, with_tone)
    if not a or b is None:
        return None
    return difflib.SequenceMatcher(None, a, b).ratio()


# ══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="TTS 音质客观测量（含 ASR 反听）")
    ap.add_argument("--dir", default=None, help="TTS 模型目录（默认自动挑）")
    ap.add_argument("--model-type", default="auto",
                    choices=("auto",) + T.SUPPORTED_TYPES)
    ap.add_argument("--lang", default=None, help="文本正则化语言（kokoro）")
    ap.add_argument("--speakers", default="0",
                    help="说话人编号，逗号分隔（如 0,3,42）；或用 --all-speakers")
    ap.add_argument("--all-speakers", action="store_true",
                    help="逐个测所有说话人（模型很多时会很慢）")
    ap.add_argument("--text", action="append", default=None,
                    help="测试文本，可重复；不给则用内置的车载播报语")
    ap.add_argument("--text-file", default=None,
                    help="从文件读测试文本（一行一句）—— 绕开命令行传中文的转义问题")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--asr", dest="use_asr", action="store_true", default=True,
                    help="用板载 ASR 反听（默认开）")
    ap.add_argument("--no-asr", dest="use_asr", action="store_false")
    ap.add_argument("--asr-dir", default=None, help="SenseVoice 模型目录")
    ap.add_argument("--out-dir", default="/tmp/tts_quality")
    args = ap.parse_args()

    if np is None:
        print("缺少 numpy，先跑：bash run_py.sh env_doctor.py")
        return 1

    texts = args.text or DEFAULT_TEXTS
    if args.text_file and os.path.isfile(args.text_file):
        with open(args.text_file, encoding="utf-8") as f:
            texts = [l.strip() for l in f if l.strip()] or texts

    # ── 加载 TTS ───────────────────────────────────────────────────
    print("=" * 78)
    print("  TTS 音质客观测量")
    print("=" * 78)
    eng = T.TtsEngine(log=lambda s: print("  [TTS] %s" % s))
    print("  正在加载 TTS…")
    ok, msg = eng.load(args.dir, args.threads,
                       model_type=args.model_type, lang=args.lang)
    print("  %s %s" % ("✅" if ok else "❌", msg))
    if not ok:
        print("\n  先跑 `bash run_py.sh tts_engine.py --check` 看诊断。")
        return 1
    print("  模型 = %s" % eng._dir)
    voices = eng.voice_names()
    if voices:
        print("  音色名 = %s%s" % (", ".join(voices[:8]),
                                   " …" if len(voices) > 8 else ""))
    print()

    speakers = (list(range(eng.num_speakers)) if args.all_speakers
                else [int(x) for x in args.speakers.split(",") if x.strip()])
    speakers = [s for s in speakers if 0 <= s < max(1, eng.num_speakers)] or [0]

    # ── 加载 ASR（可选）────────────────────────────────────────────
    asr = None
    if args.use_asr:
        try:
            import asr_engine as A
            asr = A.AsrEngine(log=lambda s: print("  [ASR] %s" % s))
            print("  正在加载 SenseVoice（约 10 秒）…")
            ok, msg = asr.load(args.asr_dir, num_threads=2)
            print("  %s %s" % ("✅" if ok else "⚠️", msg))
            if not ok:
                asr = None
        except Exception as e:
            print("  ⚠️  加载 ASR 失败，跳过回环：%s" % e)
            asr = None
    print()

    out_dir = os.path.join(args.out_dir, os.path.basename(eng._dir.rstrip("/")))
    os.makedirs(out_dir, exist_ok=True)

    # ── 逐个测 ─────────────────────────────────────────────────────
    rows = []
    for spk in speakers:
        print("─" * 78)
        print("  说话人 %d%s" % (spk, ("（%s）" % voices[spk])
                                 if spk < len(voices) else ""))
        print("─" * 78)
        for i, text in enumerate(texts):
            t0 = time.time()
            sr, data = eng.synth(text, speaker=spk, speed=1.0)
            synth_sec = time.time() - t0
            if data is None:
                print("    ❌ 合成失败：%s" % text)
                continue

            m = analyse(data, sr)
            rtf = synth_sec / m["dur"] if m["dur"] > 0 else 0.0
            name = "spk%03d_%02d.wav" % (spk, i)
            path = os.path.join(out_dir, name)
            eng._write_wav(path, sr, data)

            heard, acc, py = "", None, None
            if asr is not None:
                ok2, res = asr.recognize(sr, data)
                heard = res if ok2 else "(没听清)"
                acc = text_accuracy(text, heard)
                py = pinyin_accuracy(text, heard)

            rows.append({"spk": spk, "text": text, "rtf": rtf, "m": m,
                         "heard": heard, "acc": acc, "py": py, "path": path})

            print("    「%s」" % text)
            print("      时长 %.2fs   合成 %.2fs  RTF %.2f   峰值 %.3f  "
                  "语音占比 %.0f%%  削顶 %.2f%%" % (
                      m["dur"], synth_sec, rtf, m["peak"],
                      m["speech_ratio"] * 100, m["clip"] * 100))
            if asr is not None:
                print("      ASR 反听 →「%s」" % heard)
                # ⚠️ 指标算不出来时必须**明确说"不可用"**，绝不能显示成 0%
                #    —— 一个说谎的指标比没有指标更危险（2026-09-27 踩过）
                if py is None:
                    py_txt = "拼音 ——（不可用，见汇总提示）"
                else:
                    py_txt = "**拼音 %.0f%%**" % (py * 100)
                tip = ""
                if acc is not None and py is not None and py - acc > 0.25:
                    tip = "   ← 字符低但拼音高：**只是同音字，音频是对的**"
                print("      %s   字符 %.0f%%%s" % (py_txt, (acc or 0) * 100, tip))
        print()

    if not rows:
        print("  没有任何样本成功合成。")
        return 1

    # ── 汇总 ───────────────────────────────────────────────────────
    accs = [r["acc"] for r in rows if r["acc"] is not None]
    pys = [r["py"] for r in rows if r["py"] is not None]
    rtfs = [r["rtf"] for r in rows]
    peaks = [r["m"]["peak"] for r in rows]

    print("=" * 78)
    print("  汇总：%s  [%s]" % (os.path.basename(eng._dir.rstrip("/")), eng._type))
    print("=" * 78)
    print("  样本数        %d（%d 个说话人 × %d 句）" % (
        len(rows), len(speakers), len(texts)))
    print("  RTF           平均 %.2f   最差 %.2f   ← >1 表示慢于实时"
          % (sum(rtfs) / len(rtfs), max(rtfs)))
    print("  峰值          平均 %.3f   ← 越小越需要放大音量" % (
        sum(peaks) / len(peaks)))
    if pys:
        print("  **拼音准确率   平均 %.1f%%   最低 %.1f%%**  ← 核心指标"
              "（同音容忍，看「音对不对」）"
              % (sum(pys) / len(pys) * 100, min(pys) * 100))
        good = [r for r in rows if r["py"] is not None and r["py"] >= 0.8]
        print("  听得清（拼音≥80%%） %d / %d 条" % (len(good), len(pys)))
    elif any(r["heard"] for r in rows):
        print("  ⚠️  **拼音指标不可用** —— pypinyin 不在当前解释器里：")
        print("      %s" % sys.executable)
        print("      装上再看（注意必须装进**这个**解释器）：")
        print("      %s -m pip install pypinyin "
              "-i https://mirrors.cloud.tencent.com/pypi/simple" % sys.executable)
    if accs:
        print("  字符准确率     平均 %.1f%%   ← 严格比对；明显低于拼音 = 只是同音字"
              % (sum(accs) / len(accs) * 100))
    print()
    print("  wav 存在：%s" % out_dir)
    print("  scp 回 PC 亲耳听：scp root@<板子IP>:%s/*.wav ./" % out_dir)
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
