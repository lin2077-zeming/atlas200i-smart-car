#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kws_emergency.py —— 语音急停监听（命令行演示版，不接小车）

这是**不接小车**的独立演示：麦克风常开，听到"急停"就打印红字并响一声。
用来验证语音急停在真实环境下的表现，也方便调阈值。

接小车的版本在 car_gui_core.py / car_gui_pyside.py 里（KwsListener 的 on_hit
直接调 controller.stop()）。

用法
----
    python3 kws_emergency.py                      # 默认参数
    python3 kws_emergency.py --threshold 0.20
    python3 kws_emergency.py --device 0
    python3 kws_emergency.py --beep               # 命中时响一声（需 aplay）
    python3 kws_emergency.py --stats              # 退出时打印统计

退出：Ctrl-C
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kws_engine as K                                    # noqa: E402


def beep():
    """响一声提示（尽力而为，没有 aplay 就静默跳过）。"""
    import subprocess
    for cmd in (["aplay", "-q", "/usr/share/sounds/alsa/Front_Center.wav"],
                ["speaker-test", "-t", "sine", "-f", "880", "-l", "1"]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except Exception:
            continue


def main():
    ap = argparse.ArgumentParser(description="语音急停监听（命令行演示）")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--threshold", type=float, default=K.DEFAULT_THRESHOLD)
    ap.add_argument("--threads", type=int, default=K.DEFAULT_THREADS)
    ap.add_argument("--keywords", default=None)
    ap.add_argument("--model-dir", default=None)
    ap.add_argument("--cooldown", type=float, default=K.COOLDOWN_SECONDS)
    ap.add_argument("--beep", action="store_true", help="命中时响一声")
    ap.add_argument("--stats", action="store_true", help="退出时打印统计")
    args = ap.parse_args()

    print("=" * 60)
    print("  语音急停监听演示（不接小车 · 只打印）")
    print("=" * 60)

    dep = K.dependency_status()
    print("依赖：numpy=%s sounddevice=%s sherpa_onnx=%s(%s)" % (
        dep["numpy"], dep["sounddevice"], dep["sherpa_onnx"], dep.get("version", "-")))
    if not (dep["numpy"] and dep["sounddevice"] and dep["sherpa_onnx"]):
        need = [m for m in ("numpy", "sounddevice", "sherpa_onnx") if not dep.get(m)]
        try:
            import asr_engine as _E
            _, hint = _E.dependency_hint(need)
            print()
            print(hint)
        except Exception:
            print("依赖不全，退出。请先跑 bash install_deps.sh，"
                  "或用 python3 env_doctor.py 确认解释器。")
        return 1

    eng = K.KwsEngine(log=lambda s: print("  [KWS] %s" % s))
    print("\n加载 KWS 模型…")
    ok, msg = eng.load(kws_dir=args.model_dir, keywords_file=args.keywords,
                       num_threads=args.threads, threshold=args.threshold)
    if not ok:
        print("❌ 加载失败：%s" % msg)
        return 1
    print("✅ %s" % eng.describe())
    print("\n一级急停词：%s" % "、".join(lb for lb, _ in eng.keywords))
    print("提示：清晰、连贯地说「急停」，距离麦克风 20~50cm")

    hit_count = [0]

    def on_hit(label, ts):
        hit_count[0] += 1
        hhmmss = time.strftime("%H:%M:%S", time.localtime(ts))
        print("\n  🔴🔴🔴  [%s] 命中「%s」→ 语音急停！(第 %d 次)" % (
            hhmmss, label, hit_count[0]))
        print("        （这里在真机上会调用 controller.stop()）\n")
        if args.beep:
            beep()

    device = args.device
    if device is None:
        device = K.guess_input_device()
    print("\n录音设备：[%s]" % device)
    try:
        for d in K.list_input_devices():
            if d["index"] == device:
                print("  名称：%s  %dHz" % (d["name"], d["samplerate"]))
    except Exception:
        pass

    lis = K.KwsListener(engine=eng, device=device, on_hit=on_hit,
                        cooldown=args.cooldown, log=lambda s: print("  [KWS] %s" % s))
    ok, msg = lis.start()
    if not ok:
        print("❌ 启动失败：%s" % msg)
        return 1
    print("✅ %s" % msg)
    print("\n监听中…对着麦克风说「急停」（Ctrl-C 退出）\n")

    t0 = time.time()
    try:
        while True:
            time.sleep(0.5)
            lv = lis.level
            bar = "▮" * int(min(1.0, lv * 3.0) * 20) + "▯" * (20 - int(min(1.0, lv * 3.0) * 20))
            sys.stdout.write("\r  [%s] 电平 %.4f   已运行 %4.0fs  命中 %d 次   " % (
                bar, lv, time.time() - t0, hit_count[0]))
            sys.stdout.flush()
            if not lis.watchdog_ok():
                sys.stdout.write("\n  ⚠️  麦克风无数据！语音急停已失效\n")
    except KeyboardInterrupt:
        print("\n\n收到 Ctrl-C，停止监听…")
    finally:
        lis.stop()

    if args.stats:
        hits = lis.hits
        dur = time.time() - t0
        print("\n===== 统计 =====")
        print("  运行时长：%.1f 秒" % dur)
        print("  命中次数：%d" % len(hits))
        for lb, ts in hits:
            print("    %s  %s" % (time.strftime("%H:%M:%S", time.localtime(ts)), lb))
        if dur > 0:
            print("  平均每 %.1f 秒一次命中" % (dur / max(1, len(hits))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
