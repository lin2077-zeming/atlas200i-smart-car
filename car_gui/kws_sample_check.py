#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kws_sample_check.py —— KWS 样本录制质量体检（不接小车、不加载模型）

为什么需要它
------------
2026-09-26 第一次真人录音跑了 `kws_batch_test.py --sweep`，召回率只有 25%，
而且 **4 个阈值（0.25/0.20/0.15/0.10）结果完全相同**。

"降阈值完全无效"这件事本身就是铁证：如果漏报样本真的进了评分，降阈值一定
会让其中一部分翻盘。结果一字不差 → 说明漏报的那几条**压根没被评分**，
因为它们里面根本没有语音。

事后用能量包络 + ASR 反查确认了这一点：
  - 5 条全静音（RMS 0.003 = 纯底噪）
  - 2 条语音只有 0.12~0.18 秒
  - 多条开口太晚（1.0~1.4 秒才出声），贴到 1.8 秒窗口末尾被截断

也就是说：**问题出在录音，不在模型、也不在阈值。** 在修好录音脚本重录之前，
任何阈值扫描都是在浪费板子上的时间。

本脚本就是在**录音之后、跑批量测试之前**插一道质检闸门：不加载 KWS 模型
（秒级返回），只做纯信号分析，逐条告诉你"这条录得合不合格"。

判决依据
--------
1. **有没有录到**：整段 RMS / 峰值。太低 → 增益 0% 或没出声。
2. **说话从第几秒开始**：能量端点检测算语音区间。开口太晚 → 有效语音被浪费。
3. **有效语音多长**：太短（<0.25s）→ 说不清一个词；贴到窗口末尾 → 疑似被截断。
4. **削顶**：|x| > 0.99 的比例。太高 → 离麦太近或增益过大，波形被削平。

退出码：0 = 全部合格；1 = 有不合格项（方便脚本串联）。

用法
----
    bash run_py.sh kws_sample_check.py                  # 体检 ./kws_samples
    bash run_py.sh kws_sample_check.py --dir kws_samples
    bash run_py.sh kws_sample_check.py --verbose        # 打印能量包络缩略图
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asr_engine as E                                    # noqa: E402

try:
    import numpy as np
except Exception:
    np = None

# ── 判定阈值（可按现场底噪调整）─────────────────────────────────────
RMS_SILENT = 0.006        # 整段 RMS 低于此值 → 判定"全静音"
PEAK_SILENT = 0.020       # 峰值低于此值 → 判定"没录到"
VOICE_RMS_RATIO = 4.0     # 帧 RMS 超过底噪的多少倍算"在说话"
GAP_MERGE_SEC = 0.30      # 语音段间静音短于此 → 合并（音节间自然停顿）
VOICE_MIN_SEC = 0.25      # 有效语音短于此 → 太短，说不清一个词
ONSET_LATE_SEC = 0.80     # 语音起点晚于此 → 开口太晚
TAIL_MARGIN_SEC = 0.05    # 语音终点距窗口末尾小于此值 → 疑似截断
CLIP_RATIO = 0.001        # |x|>0.99 的样本占比上限


def read_wav(path):
    """读 wav，返回 (rate, float32 单声道)。"""
    import wave
    with wave.open(path, "rb") as f:
        nch = f.getnchannels()
        width = f.getsampwidth()
        rate = f.getframerate()
        frames = f.readframes(f.getnframes())
    if width != 2:
        raise ValueError("只支持 16bit wav（当前 %d bit）" % (width * 8))
    a = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return rate, a


def voice_span(a, rate, frame_ms=20):
    """
    用能量端点检测找语音区间，返回 (起点秒, 终点秒, 帧 RMS 数组, 底噪)。

    底噪取**全部帧 RMS 的低分位数**（20%），比"取前 5 帧"稳——
    因为很多样本一开头就有人说话，用开头估底噪会把语音当成底噪。

    ⚠️ 必须**合并间隔 < GAP_MERGE_SEC 的相邻段**：说话时音节之间必然掉到
    阈值以下（"急停"里 j 和 t 之间有停顿），只取最长连续段会把 1 秒的话
    误判成 0.2 秒。逻辑与 kws_record.voice_span 保持一致。
    """
    n = max(1, int(rate * frame_ms / 1000.0))
    nfr = len(a) // n
    if nfr < 2:
        return 0.0, len(a) / float(rate), np.zeros(0, dtype=np.float32), 0.0
    frames = a[:nfr * n].reshape(nfr, n)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    noise = float(np.percentile(rms, 20))
    thr = max(noise * VOICE_RMS_RATIO, RMS_SILENT)
    voiced = rms > thr
    if not voiced.any():
        return -1.0, -1.0, rms, noise

    gap_frames = max(1, int(GAP_MERGE_SEC * 1000.0 / frame_ms))
    idx = np.where(voiced)[0]
    segs = []
    seg_s = seg_e = int(idx[0])
    for k in range(1, len(idx)):
        i = int(idx[k])
        if i - seg_e <= gap_frames:
            seg_e = i
        else:
            segs.append((seg_s, seg_e))
            seg_s = seg_e = i
    segs.append((seg_s, seg_e))
    best_s, best_e = max(segs, key=lambda s: s[1] - s[0])
    return (best_s * n / float(rate), (best_e + 1) * n / float(rate), rms, noise)


def bar(v, width=30, scale=10.0):
    """把 RMS 画成一条横条（scale 倍放大便于肉眼看）。"""
    n = int(min(1.0, v * scale) * width)
    return "█" * n + "·" * (width - n)


def check_one(path, verbose=False):
    """体检一条 wav，返回 (是否合格, 问题列表, 指标 dict)。"""
    name = os.path.basename(path)
    try:
        rate, a = read_wav(path)
    except Exception as e:
        return False, ["读文件失败：%s" % e], {}

    dur = len(a) / float(rate)
    rms = float(np.sqrt(np.mean(a * a) + 1e-12))
    peak = float(np.max(np.abs(a))) if len(a) else 0.0
    clip = float(np.mean(np.abs(a) > 0.99)) if len(a) else 0.0
    onset, offset, frms, noise = voice_span(a, rate)
    vdur = (offset - onset) if onset >= 0 else 0.0

    problems = []
    if rms < RMS_SILENT or peak < PEAK_SILENT:
        problems.append("全静音：没录到声音（RMS %.4f 峰值 %.4f）—— 重录这条"
                        % (rms, peak))
    else:
        if vdur < VOICE_MIN_SEC:
            problems.append("有效语音只有 %.2fs（<%.2fs），太短 —— 重录这条"
                            % (vdur, VOICE_MIN_SEC))
        if onset > ONSET_LATE_SEC:
            problems.append("第 %.2fs 才开口（>%.2fs），前面都在等 —— 重录这条"
                            % (onset, ONSET_LATE_SEC))
        if offset >= 0 and (dur - offset) < TAIL_MARGIN_SEC:
            problems.append("语音贴到窗口末尾（终点 %.2fs / 总长 %.2fs），"
                            "疑似被截断 —— 建议加大 --window" % (offset, dur))
    if clip > CLIP_RATIO:
        problems.append("削顶：%.2f%% 的样本触顶（离麦太近或增益过大）"
                        % (clip * 100))

    m = {"name": name, "rate": rate, "dur": dur, "rms": rms, "peak": peak,
         "clip": clip, "onset": onset, "offset": offset, "vdur": vdur,
         "noise": noise, "rms_arr": frms}
    if verbose and len(frms):
        print("      包络：%s" % bar(float(np.max(frms))))
        step = max(1, len(frms) // 60)
        print("      逐帧：%s" % bar(float(np.mean(frms)), width=60, scale=30))
    return (not problems), problems, m


def main():
    ap = argparse.ArgumentParser(description="KWS 样本录制质量体检")
    ap.add_argument("--dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "kws_samples"))
    ap.add_argument("--verbose", action="store_true", help="打印能量包络")
    args = ap.parse_args()

    if np is None:
        ok, hint = E.dependency_hint(["numpy"])
        print("缺少 numpy。\n")
        print(hint)
        return 1
    if not os.path.isdir(args.dir):
        print("样本目录不存在：%s" % args.dir)
        print("先跑：bash run_py.sh kws_record.py")
        return 1

    files = sorted(f for f in os.listdir(args.dir) if f.lower().endswith(".wav"))
    if not files:
        print("目录里没有 wav：%s" % args.dir)
        return 1

    print("=" * 76)
    print("  KWS 样本录制质量体检")
    print("=" * 76)
    print("  目录：%s" % args.dir)
    print("  共 %d 条。判定线：语音 ≥%.2fs、起点 ≤%.2fs、不贴窗口末尾、无削顶"
          % (len(files), VOICE_MIN_SEC, ONSET_LATE_SEC))
    print()

    bad = []
    for f in files:
        path = os.path.join(args.dir, f)
        good, problems, m = check_one(path, verbose=args.verbose)
        if m:
            print("  %-26s %5.2fs  RMS %.4f  峰值 %.4f  语音 %.2f~%.2fs (%.2fs)  %s"
                  % (m["name"], m["dur"], m["rms"], m["peak"],
                     max(0.0, m["onset"]), max(0.0, m["offset"]), m["vdur"],
                     "✅" if good else "❌"))
        else:
            print("  %-26s ❌ 读取失败" % f)
        for p in problems:
            print("        ❌ %s" % p)
        if not good:
            bad.append(f)

    print()
    print("=" * 76)
    if bad:
        print("  结论：%d / %d 条不合格，**先重录再跑批量测试**。" % (len(bad), len(files)))
        print()
        print("  不合格清单：")
        for f in bad:
            print("     - %s" % f)
        print()
        print("  ⚠️  重录之前请先确认 kws_record.py 是新版（带倒计时 + 自动截取）。")
        print("     旧版是定长硬录，就是这次 25% 召回率的根因。")
        print()
        print("  重录：")
        print("     bash run_py.sh kws_record.py --redo        # 只重录不合格的")
        print("     或")
        print("     bash run_py.sh kws_record.py               # 全量重录")
    else:
        print("  结论：%d 条全部合格 ✅" % len(files))
        print()
        print("  下一步（标定阈值）：")
        print("     bash run_py.sh kws_batch_test.py --sweep")
    print("=" * 76)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
