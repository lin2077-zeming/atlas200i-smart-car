#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用真实人声样本测「ASR 识别 → 规则/LLM 处理」全链路。

不接小车、用离线演示模式，**不会动车**。
目的：找出**真实 ASR 误识别**，看 LLM 能否纠对。
"""
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = sorted(glob.glob(os.path.join(HERE, "kws_samples", "*.wav")))

print("=" * 76)
print(" 真实人声样本：ASR 识别 → 规则/LLM 处理链路")
print("=" * 76)
print("样本数：%d" % len(SAMPLES))
if not SAMPLES:
    print("!! kws_samples/ 里没有 wav，无法测试")
    raise SystemExit(1)

from asr_engine import AsrEngine          # noqa: E402
import car_gui_core as C                  # noqa: E402

eng = AsrEngine()
ok, msg = eng.load()
print("ASR 加载：%s —— %s" % (ok, msg))
if not ok:
    print("!! ASR 不可用，退出")
    raise SystemExit(1)

print()
print("-" * 76)

rows = []
for p in SAMPLES:
    name = os.path.basename(p)
    try:
        # 契约：recognize_file() 返回 (是否成功, 文本或错误) —— 与 recognize() 一致
        ok_rec, text = eng.recognize_file(p)
        text = (text or "").strip()
        if not ok_rec:
            text = "<未识别:%s>" % text
    except Exception as e:
        text = "<失败:%r>" % (e,)
    res, msg = C.parse_text_command(text, 300)
    hit = res is not None
    act = res.get("action") if res else "-"
    rows.append((name, text, hit, act))
    print("%-22s ASR=%-20r 规则=%-6s %s"
          % (name, text, "命中" if hit else "未命中", act if hit else ""))

print()
print("=" * 76)
print(" 汇总")
print("=" * 76)
hit = sum(1 for r in rows if r[2])
print("规则快路径命中：%d / %d" % (hit, len(rows)))
print()
print("未命中（这些才会走 LLM）：")
miss = [r for r in rows if not r[2]]
if not miss:
    print("   （无）")
for name, text, _, _ in miss:
    print("   %-22s %r" % (name, text))
