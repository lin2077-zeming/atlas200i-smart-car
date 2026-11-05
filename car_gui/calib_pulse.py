#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极短脉冲速度标定（安全版）—— 找出"命令值 → 实际速度"的真实映射。

背景（2026-10-17 教训）：
  一直假设下位机速度单位是 **mm/s**，用 50 跑 4 秒以为只走 200mm，
  结果**车跑得飞快**。⇒ 这个假设是错的，单位很可能大一个数量级。
  项目记忆里"速度是否真为 mm/s"本来就是 ⏸ 未验证项，现在必须测出来。

安全设计：
  · 每个脉冲**最长 0.5 秒**，跑完立刻连发停车帧；
  · 脉冲之间静置等待，给人反应时间；
  · 直接调 `_write_speed()` 写**原始值**，绕过 `_clamp_speed` 的
    `SPEED_MIN=50` 下限（否则测不到更小的值）；
  · 硬上限：单脉冲 ≤1.0s、原始速度 ≤100（超过直接拒绝）。

用法：
  bash run_py.sh calib_pulse.py --pulses 5,10,20,30,50 --dur 0.5
"""
import argparse
import sys
import time

sys.path.insert(0, "/home/HwHiAiUser/car_gui")
import car_gui_core as C                                    # noqa: E402

MAX_DUR = 1.0          # 单脉冲时长硬上限（秒）
MAX_RAW = 100          # 原始速度硬上限


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pulses", default="5,10,20,30,50",
                    help="逗号分隔的原始速度值（≤%d）" % MAX_RAW)
    ap.add_argument("--dur", type=float, default=0.5,
                    help="每个脉冲时长（秒，≤%.1f）" % MAX_DUR)
    ap.add_argument("--pause", type=float, default=2.5,
                    help="脉冲之间静置秒数")
    a = ap.parse_args()

    try:
        pulses = [int(x) for x in a.pulses.split(",") if x.strip()]
    except ValueError:
        print("--pulses 格式不对，例：5,10,20,30,50")
        return 2
    if a.dur > MAX_DUR:
        print("❌ 拒绝：--dur %.2f 超过硬上限 %.1fs" % (a.dur, MAX_DUR))
        return 2
    bad = [p for p in pulses if p > MAX_RAW or p < 0]
    if bad:
        print("❌ 拒绝：这些速度超过硬上限 %d：%s" % (MAX_RAW, bad))
        return 2

    cc = C.CarController()
    cc._announce_actions = False
    print("=" * 66)
    print(" 极短脉冲速度标定（安全版）")
    print("   每个脉冲 %.2f 秒，随后连发停车；脉冲之间静置 %.1fs" % (a.dur, a.pause))
    print("   原始速度序列: %s" % pulses)
    print("   参考：若单位真是 mm/s，速度 50 × 0.5s = 25mm（几乎看不出动）")
    print("=" * 66)
    ok, msg = cc.connect()
    print("  连接:", ok, msg)
    if not ok:
        return 1
    print("  电压:", cc.status().get("voltage"), "V")
    print()
    print("  ⚠ 请站在车侧、留出前方空间、手边能拔电池")
    print("  📏 每跑一个脉冲，记一下车大约走了多远（哪怕只是「动了没动」）")
    print()
    for i in (5, 4, 3, 2, 1):
        print("     %d…" % i, flush=True)
        time.sleep(1.0)

    results = []
    try:
        for n, v in enumerate(pulses, 1):
            print()
            print("─" * 66)
            print(" [%d/%d] 原始速度 = %d，持续 %.2f 秒" % (n, len(pulses), v, a.dur))
            t0 = time.time()
            try:
                cc._write_speed(v, 0, 0)          # ★ 直接写原始值，绕过 clamp
            except Exception as e:
                print("   下发异常：%r" % (e,))
                break
            time.sleep(a.dur)
            # ★ 读一下下位机回报的 vx（它自己认为的速度），有助于判断单位
            try:
                fb = getattr(cc, "_last_fb", None) or {}
                vx = fb.get("vx")
            except Exception:
                vx = None
            # 连发停车帧，确保一定停
            for _ in range(5):
                try:
                    cc._write_speed(0, 0, 0)
                except Exception:
                    pass
                time.sleep(0.02)
            dt = time.time() - t0
            print("   ▶ 已停（脉冲 %.2fs，含停车 ≈%.2fs）；下位机回报 vx=%s"
                  % (a.dur, dt, vx))
            results.append(v)
            if n < len(pulses):
                print("   ⏸ 静置 %.1fs…（此时可量距离/做标记）" % a.pause)
                time.sleep(a.pause)
    finally:
        print()
        print("=" * 66)
        print(" 收尾：连发停车 + 断开")
        try:
            for _ in range(6):
                cc._write_speed(0, 0, 0)
                time.sleep(0.02)
        except Exception:
            pass
        try:
            cc.disconnect()
        except Exception:
            pass
        print("  已跑过的原始速度: %s" % results)
        print("  请把每个值对应的「大概走了多远」告诉我，我来反算真实单位。")
        print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
