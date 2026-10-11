#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tts_speaker_compare.py —— 批量合成多个说话人，拼成**一个**对比 wav

为什么需要它
------------
当前模型有几十上百个说话人（旧模型 187，Kokoro 54）。要给小车挑一个播报音色，
逐个合成再 scp 回 PC 听，要来回传上百次 —— 不现实。

这个脚本把若干个候选音色**拼成一个 wav**（中间插静音间隔），
你只需要传**一个文件**回 PC，一次听完再定。

用法
----
    # 默认挑 6 个候选，说"已急停"
    bash run_py.sh tts_speaker_compare.py

    # 指定文本和说话人
    bash run_py.sh tts_speaker_compare.py --text "前方有障碍，已停止" \\
        --speakers 0,3,5,18,42,99 --out /tmp/cmp.wav

    # 随机挑 8 个（懒得想编号时）
    bash run_py.sh tts_speaker_compare.py --random 8

    # 每句前不加编号播报（默认会先合成"编号 N"，方便你对着听）
    bash run_py.sh tts_speaker_compare.py --no-announce

在 PC 上取回：
    scp root@192.168.137.100:/tmp/tts_compare.wav ./
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import numpy as np
except Exception:
    np = None

# 默认候选：分散在 0~186 区间，覆盖不同音色
DEFAULT_SPEAKERS = (0, 3, 5, 18, 42, 99)


def concat(parts, rate, gap_sec=0.6):
    """把若干 float32 数组用静音拼起来。"""
    gap = np.zeros(int(rate * gap_sec), dtype=np.float32)
    out = []
    for i, a in enumerate(parts):
        if i:
            out.append(gap)
        out.append(a)
    return np.concatenate(out)


def write_wav(path, rate, data):
    import wave
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with wave.open(path, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(int(rate))
        f.writeframes(pcm.tobytes())


def main():
    ap = argparse.ArgumentParser(description="批量合成多说话人并拼成一个对比 wav")
    ap.add_argument("--text", default="已急停", help="要合成的文本")
    ap.add_argument("--speakers", default=None,
                    help="逗号分隔的说话人编号，如 0,3,5,18（默认 %s）"
                         % ",".join(str(s) for s in DEFAULT_SPEAKERS))
    ap.add_argument("--random", type=int, default=0,
                    help="随机挑 N 个说话人（与 --speakers 二选一）")
    ap.add_argument("--out", default="/tmp/tts_compare.wav", help="输出路径")
    ap.add_argument("--speed", type=float, default=1.0, help="语速")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--dir", default=None, help="TTS 模型目录")
    ap.add_argument("--announce", dest="announce", action="store_true",
                    default=True, help="每句前先念出编号（默认开）")
    ap.add_argument("--no-announce", dest="announce", action="store_false",
                    help="不要念编号")
    args = ap.parse_args()

    if np is None:
        print("缺少 numpy。")
        return 1

    import tts_engine as T

    eng = T.TtsEngine(log=lambda s: print("  [TTS] %s" % s))
    ok, msg = eng.load(args.dir, args.threads)
    print("加载：%s" % msg)
    if not ok:
        print("说明：TTS 模型缺失或依赖不全。见《13.语音合成TTS接入操作文档.md》")
        return 1

    total = max(1, eng.num_speakers or 0)

    # 🔴 多语言模型（如 Kokoro）里只有**一小段编号**是中文音色，其余是英/日/韩/西语。
    #    默认对比如果不落在那一段里，比出来全是外语音色，白挑一场。
    zh = T.zh_speaker_range(eng._dir)

    if args.random:
        rng = np.random.default_rng()
        pool = list(range(zh[0], zh[1] + 1)) if zh else list(range(total))
        spk = sorted(int(x) for x in rng.choice(
            pool, size=min(args.random, len(pool)), replace=False))
    elif args.speakers:
        spk = [int(x) for x in args.speakers.split(",") if x.strip() != ""]
    elif zh:
        lo, hi = zh
        n = hi - lo + 1
        spk = sorted({lo + int(round(i * (n - 1) / 5.0)) for i in range(6)})
        print("（多语言模型：中文音色只有 %d~%d，默认就在这一段里挑）" % (lo, hi))
    else:
        spk = [s for s in DEFAULT_SPEAKERS if s < total]

    bad = [s for s in spk if not (0 <= s < total)]
    if bad:
        print("❌ 说话人编号超出范围（0~%d）：%s" % (total - 1, bad))
        return 1

    print("共 %d 个说话人可选；本次对比 %d 个：%s"
          % (total, len(spk), ",".join(str(s) for s in spk)))
    print("文本：%s" % args.text)

    rate = eng.sample_rate or 16000
    parts = []
    for s in spk:
        if args.announce:
            # 先念编号，方便你边听边记「第几个好听」
            sr, ann = eng.synth("编号 %d" % s, speaker=s, speed=args.speed)
            if ann is not None:
                parts.append(ann)
                rate = sr or rate
        sr, a = eng.synth(args.text, speaker=s, speed=args.speed)
        if a is None:
            print("  ⚠️  说话人 %d 合成失败，跳过" % s)
            continue
        rate = sr or rate
        parts.append(a)
        print("  ✅ 说话人 %3d  合成 %.2f 秒" % (s, len(a) / float(rate)))

    if not parts:
        print("❌ 全部合成失败")
        return 1

    data = concat(parts, rate)
    write_wav(args.out, rate, data)
    print("\n已写出：%s（共 %.1f 秒，含 %d 个候选）"
          % (args.out, len(data) / float(rate), len(spk)))
    print("\n在 PC 上取回后播放：")
    print("   scp root@<板子IP>:%s ./" % args.out)
    if args.announce:
        print("   （每段前会先念出编号，听到喜欢的记下那个数字）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
