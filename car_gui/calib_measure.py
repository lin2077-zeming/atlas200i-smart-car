#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单次定向标定：用指定原始速度跑指定时长，让用户量距离。

安全限制：raw ≤ 100，dur ≤ 5.0 秒。
"""
import argparse
import sys
import time

sys.path.insert(0, "/home/HwHiAiUser/car_gui")
import car_gui_core as C                                    # noqa: E402

MAX_DUR = 5.0
MAX_RAW = 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=int, required=True)
    ap.add_argument("--dur", type=float, required=True)
    a = ap.parse_args()
    if not (0 <= a.raw <= MAX_RAW):
        print("❌ --raw 必须在 0..%d" % MAX_RAW)
        return 2
    if not (0 < a.dur <= MAX_DUR):
        print("❌ --dur 必须在 (0, %.1f]" % MAX_DUR)
        return 2

    cc = C.CarController()
    cc._announce_actions = False
    ok, msg = cc.connect()
    print("连接:", ok, msg)
    if not ok:
        return 1
    print("电压:", cc.status().get("voltage"), "V")
    print()
    print("=" * 66)
    print(" 【距离标定】raw=%d，持续 %.2f 秒" % (a.raw, a.dur))
    print()
    print("   👉 请**现在**在车头正前方地面做一条标记（比如放个硬币/纸片）")
    print("   👉 跑完你自己量：车头移动了多少毫米")
    print()
    print("   理论参考（若单位真是 mm/s）：%.2f 秒 × %d = %.0f mm"
          % (a.dur, a.raw, a.dur * a.raw))
    print("=" * 66)
    for i in (5, 4, 3, 2, 1):
        print("     %d…" % i, flush=True)
        time.sleep(1.0)

    try:
        print("   ▶ 开始…", flush=True)
        cc._write_speed(a.raw, 0, 0)
        t0 = time.time()
        while time.time() - t0 < a.dur:
            time.sleep(0.05)
        for _ in range(6):
            try:
                cc._write_speed(0, 0, 0)
            except Exception:
                pass
            time.sleep(0.02)
        print("   ■ 已停（实际下发 %.2fs）" % (time.time() - t0))
    finally:
        try:
            cc.disconnect()
        except Exception:
            pass
        print()
        print("   👉 现在量：车头从标记线走了多少 mm？告诉我这个数。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
