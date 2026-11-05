#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转向标定（原地旋转，比前进安全）。

背景：
    2026-10-17 真车发现"先前进半米，再左转30度"计划执行时**只前进、没左转** ——
    说明 Z 轴在低命令值下也进不了死区，或者 1 秒太短（平移加速要 3.1 秒）。

本脚本按"命令值列表 × 时长"依次试点，每个点之间留足时间观察。
**原地旋转**只需车身范围，不需要走廊长度。

用法：
  bash run_py.sh calib_turn.py --cmds 100,200 --dur 1.5
"""
import argparse
import sys
import time

sys.path.insert(0, "/home/HwHiAiUser/car_gui")
import car_gui_core as C                                    # noqa: E402

MAX_DUR = 3.0
MAX_CMD = 500


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmds", default="100,200", help="逗号分隔的命令值")
    ap.add_argument("--dur", type=float, default=1.5, help="每个点转多久（≤3s）")
    ap.add_argument("--dir", default="left", choices=["left", "right"])
    a = ap.parse_args()
    try:
        cmds = [int(x) for x in a.cmds.split(",") if x.strip()]
    except ValueError:
        print("--cmds 格式不对")
        return 2
    if a.dur > MAX_DUR or any(c < 0 or c > MAX_CMD for c in cmds):
        print("❌ 参数超限")
        return 2

    act = "turn_left" if a.dir == "left" else "turn_right"
    cc = C.CarController()
    cc._announce_actions = False
    print("=" * 66)
    print(" 转向标定：%s，每个命令值转 %.2f 秒" % (act, a.dur))
    print(" 命令值序列: %s" % cmds)
    print("=" * 66)
    ok, msg = cc.connect()
    print("  连接:", ok, msg)
    if not ok:
        return 1
    print("  电压:", cc.status().get("voltage"), "V")
    print()
    print("  ⚠ 原地旋转，只需车身范围；手边能拔电池")
    print("  👀 每转一下，记一下：转了没有？大概多少度？")
    for i in (5, 4, 3, 2, 1):
        print("     %d…" % i, flush=True)
        time.sleep(1.0)

    try:
        for n, cmd in enumerate(cmds, 1):
            print()
            print("─" * 66)
            print(" [%d/%d] 命令值 %d，转 %.2f 秒" % (n, len(cmds), cmd, a.dur))
            print("   旧系数预测: %.0f°   理论系数预测: %.0f°"
                  % (cmd * C.DEG_PER_SEC_PER_UNIT * a.dur,
                     cmd * C.DEG_PER_SEC_PER_UNIT_RAD * a.dur))
            try:
                cc.move(act, cmd, a.dur)      # 走 move()，与生产路径一致
            except Exception as e:
                print("   下发异常：%r" % (e,))
                break
            print("   ▶ 转动中…", flush=True)
            time.sleep(a.dur + 1.0)
            for _ in range(6):
                try:
                    cc._write_speed(0, 0, 0)
                except Exception:
                    pass
                time.sleep(0.02)
            print("   ■ 已停")
            if n < len(cmds):
                print("   ⏸ 静置 4s（此时观察角度）…")
                time.sleep(4.0)
    finally:
        try:
            for _ in range(6):
                cc._write_speed(0, 0, 0)
                time.sleep(0.02)
            cc.disconnect()
        except Exception:
            pass
        print()
        print("=" * 66)
        print(" 请告诉我：每个命令值转了多少度（或「没转 / 转一点点 / 转很多」）")
        print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
