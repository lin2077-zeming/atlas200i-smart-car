#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""速度单位探针：发一个**极短**脉冲，连续采样下位机回报的 vx/vz。

目的：不依赖"量距离"就能判断固件的速度单位。
  · 若回报 vx ≈ 命令值        → 单位就是 mm/s（那"太快"另有原因）
  · 若回报 vx ≈ 命令值 × 10   → 单位是 cm/s 之类
  · 若回报恒为 0              → 固件不发速度反馈，只能靠量距离

安全：单脉冲 ≤1.2s，立即连发停车。
"""
import argparse
import sys
import time

sys.path.insert(0, "/home/HwHiAiUser/car_gui")
import car_gui_core as C                                    # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=int, default=50, help="原始速度值（≤100）")
    ap.add_argument("--dur", type=float, default=1.0, help="脉冲秒数（≤1.2）")
    a = ap.parse_args()
    if a.raw < 0 or a.raw > 100:
        print("❌ --raw 必须在 0..100")
        return 2
    if a.dur > 1.2:
        print("❌ --dur 必须 ≤1.2s")
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
    print(" 探针：原始速度 %d，持续 %.2fs，连续采样 vx/vz" % (a.raw, a.dur))
    print(" 若回报 vx 恒为 0 → 无速度反馈，改用量距离")
    print("=" * 66)
    print("  ⚠ 请确认前方有空间，手边能拔电池")
    for i in (3, 2, 1):
        print("     %d…" % i, flush=True)
        time.sleep(1.0)

    samples = []
    try:
        cc._write_speed(a.raw, 0, 0)
        t0 = time.time()
        while time.time() - t0 < a.dur:
            time.sleep(0.05)
            try:
                st = cc.status()
                samples.append((time.time() - t0, st.get("vx"), st.get("vz"),
                                st.get("voltage")))
            except Exception:
                pass
        # 连发停车
        for _ in range(6):
            try:
                cc._write_speed(0, 0, 0)
            except Exception:
                pass
            time.sleep(0.02)
    finally:
        print()
        print("  采样（t, vx, vz, 电压）:")
        for t, vx, vz, v in samples:
            print("    t=%.2fs  vx=%-6s vz=%-6s V=%s" % (t, vx, vz, v))
        print()
        vxs = [s[1] for s in samples if s[1] is not None]
        if not vxs or all(v == 0 for v in vxs):
            print("  → vx 恒为 0：**下位机不发速度反馈**，只能靠量距离标定")
        else:
            print("  → vx 非零！最大 %s、中位 %s" %
                  (max(vxs), sorted(vxs)[len(vxs) // 2]))
            print("     命令值=%d；若 vx≈命令值 → 单位是 mm/s" % a.raw)
            print("     若 vx≈命令值×N → 说明命令单位比 mm/s 大 N 倍")
        try:
            cc.disconnect()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
