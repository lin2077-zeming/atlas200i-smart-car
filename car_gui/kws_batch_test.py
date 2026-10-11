#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kws_batch_test.py —— KWS 批量回归测试

把 kws_record.py 录下来的样本跑一遍，统计：
    - 正样本命中率（Recall）：说了"急停"能不能识别出来
    - 负样本误报率（False Positive）：说"前进"会不会被误判成急停
这两项是语音急停的**保命指标**，必须实测，不能凭感觉。

用法
----
    python3 kws_batch_test.py                      # 默认跑 ./kws_samples
    python3 kws_batch_test.py --dir kws_samples
    python3 kws_batch_test.py --threshold 0.20     # 扫阈值
    python3 kws_batch_test.py --sweep              # 扫一遍常用阈值，找最佳点
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kws_engine as K                                    # noqa: E402

# 文件名前缀 -> 是否应当命中
POS_PREFIXES = ("pos_",)
# neg_ 开头的应当是负样本


def classify(name):
    if name.startswith(POS_PREFIXES):
        return True
    if name.startswith("neg_"):
        return False
    return None


def run_once(eng, samples):
    """
    跑一遍全部样本。

    samples: [(文件名, 期望命中bool 或 None, 完整路径), ...]
    返回 (明细, 统计)
    """
    detail = []
    tp = fn = tn = fp = 0
    for fn_name, expect, path in samples:
        hits = eng.detect_file(path)
        got = bool(hits)
        if expect is None:
            verdict = "?"
        elif expect and got:
            verdict = "✅TP (正确命中)"; tp += 1
        elif expect and not got:
            verdict = "❌FN (漏报！)"; fn += 1
        elif (not expect) and got:
            verdict = "❌FP (误报！)"; fp += 1
        else:
            verdict = "✅TN (正确未命中)"; tn += 1
        detail.append((fn_name, expect, hits, verdict))
    stats = {"tp": tp, "fn": fn, "tn": tn, "fp": fp}
    return detail, stats


def summarize(stats):
    tp, fn, tn, fp = stats["tp"], stats["fn"], stats["tn"], stats["fp"]
    n_pos = tp + fn
    n_neg = tn + fp
    recall = (tp / float(n_pos)) if n_pos else 0.0
    fpr = (fp / float(n_neg)) if n_neg else 0.0
    print("  正样本 %d 条：命中 %d，漏报 %d   → 召回率 %.1f%%" % (
        n_pos, tp, fn, recall * 100))
    print("  负样本 %d 条：正确 %d，误报 %d   → 误报率 %.1f%%" % (
        n_neg, tn, fp, fpr * 100))
    if fn:
        print("  ⚠️  有漏报！语音急停漏报 = 刹不住车，必须处理")
    if fp:
        print("  ⚠️  有误报！行驶中误触发急停 = 突然停车，需评估风险")
    return recall, fpr


def main():
    ap = argparse.ArgumentParser(description="KWS 批量回归测试")
    ap.add_argument("--dir", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "kws_samples"))
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--keywords", default=None)
    ap.add_argument("--threshold", type=float, default=K.DEFAULT_THRESHOLD)
    ap.add_argument("--threads", type=int, default=K.DEFAULT_THREADS)
    ap.add_argument("--sweep", action="store_true", help="扫一遍常用阈值")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print("样本目录不存在：%s" % args.dir)
        print("先跑：bash run_py.sh kws_record.py")
        return 1

    files = sorted(f for f in os.listdir(args.dir) if f.lower().endswith(".wav"))
    if not files:
        print("目录里没有 wav：%s" % args.dir)
        return 1

    samples = []
    for f in files:
        expect = classify(f)
        samples.append((f, expect, os.path.join(args.dir, f)))

    print("样本目录：%s" % args.dir)
    print("共 %d 个 wav" % len(samples))
    for f, expect, _ in samples:
        tag = "正样本" if expect else ("负样本" if expect is False else "未标注")
        print("   %-28s %s" % (f, tag))

    thresholds = [0.25, 0.20, 0.15, 0.10] if args.sweep else [args.threshold]

    best = None
    for thr in thresholds:
        print("\n===== threshold = %.2f =====" % thr)
        eng = K.KwsEngine()
        ok, msg = eng.load(kws_dir=args.model_dir, keywords_file=args.keywords,
                           num_threads=args.threads, threshold=thr)
        if not ok:
            print("  加载失败：%s" % msg)
            return 1
        detail, stats = run_once(eng, samples)
        for name, expect, hits, verdict in detail:
            print("  %-26s %-24s %s" % (name, verdict, hits if hits else ""))
        recall, fpr = summarize(stats)
        score = recall - fpr
        print("  综合评分（召回 - 误报）= %.3f" % score)
        if best is None or score > best[0]:
            best = (score, thr, recall, fpr)

    if args.sweep:
        print("\n===== 最佳阈值 =====")
        print("  threshold = %.2f  （召回 %.1f%% / 误报 %.1f%%）" % (
            best[1], best[2] * 100, best[3] * 100))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
