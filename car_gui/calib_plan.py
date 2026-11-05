#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转向系数 / 距离 标定（真车，需地面 + 量角器/卷尺）。

用法（板上）：
  bash run_py.sh calib_plan.py --turn 1.0 --speed 300     # 测转角
  bash run_py.sh calib_plan.py --forward 2.0 --speed 300  # 测距离
  bash run_py.sh calib_plan.py --both --speed 300         # 先转角后距离

为什么必须单独写一个脚本、不能走 parse_plan：
  `parse_plan()` 对角度做了 90° 限幅（防止用户一句话转太猛），
  但**标定需要下发真实时长**，所以这里直接调 `move()` 绕过限幅。
"""
import argparse
import sys
import time

sys.path.insert(0, "/home/HwHiAiUser/car_gui")
import car_gui_core as C                                     # noqa: E402

OLD_COEF = C.DEG_PER_SEC_PER_UNIT            # 现用（0.375）
RAD_COEF = C.DEG_PER_SEC_PER_UNIT_RAD        # 手册理论（0.0573）


def do_turn(cc, speed, dur):
    print("─" * 64)
    print(" 【转角标定】")
    print("   下发：turn_left  speed=%d mm/s  duration=%.2f s" % (speed, dur))
    print()
    print("   预测（用现用系数 %.4f）： 360 度/秒 × %.2f = **%.1f 度**"
          % (OLD_COEF, speed * OLD_COEF, dur * speed * OLD_COEF))
    print("   预测（用手册理论 %.4f）： %.2f 度/秒 × %.2f = **%.1f 度**"
          % (RAD_COEF, speed * RAD_COEF, dur * speed * RAD_COEF))
    print()
    print("   >> 请用量角器量：车头相对起点转了多少度（绝对角度）")
    print("   >> 然后 3 秒后开始转向…")
    for i in (3, 2, 1):
        print("      %d…" % i, flush=True)
        time.sleep(1.0)
    cc._estop_locked = False
    cc.move("turn_left", speed, dur)
    print("   ▶ 转动中…", flush=True)
    time.sleep(dur + 0.8)
    cc.stop()
    print("   ■ 已停")
    print("   >> 现在量吧。量完告诉我实际度数，我来反算系数。")
    print("─" * 64)


def do_forward(cc, speed, dur):
    print("─" * 64)
    print(" 【距离标定】")
    print("   下发：forward  speed=%d mm/s  duration=%.2f s" % (speed, dur))
    print("   理论距离（理想匀速）= %d × %.2f = **%d mm**"
          % (speed, dur, speed * dur))
    print("   注意：电机起速约 2 秒，实际会**偏短**（项目实测通常少 10~30%%）")
    print()
    print("   >> 请在起点做个标记，量车头前进的实际距离（mm）")
    print("   >> 3 秒后开始…")
    for i in (3, 2, 1):
        print("      %d…" % i, flush=True)
        time.sleep(1.0)
    cc._estop_locked = False
    cc.move("forward", speed, dur)
    print("   ▶ 前进中…", flush=True)
    time.sleep(dur + 0.8)
    cc.stop()
    print("   ■ 已停")
    print("   >> 现在量吧。")
    print("─" * 64)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turn", type=float, default=None, help="转入时长（秒）")
    ap.add_argument("--forward", type=float, default=None, help="前进时长（秒）")
    ap.add_argument("--both", action="store_true", help="先转角再前进")
    ap.add_argument("--speed", type=int, default=300)
    ap.add_argument("--port", default=None)
    a = ap.parse_args()

    if not (a.turn or a.forward or a.both):
        print("请给 --turn / --forward / --both")
        return 2

    cc = C.CarController()
    cc._announce_actions = False
    print("=" * 64)
    print(" 转向/距离标定   车轮必须着地、周围空旷、随时能断电")
    print("=" * 64)
    ok, msg = cc.connect(port=a.port)
    print("  连接:", ok, msg)
    if not ok:
        return 1
    print("  当前电压:", cc.status().get("voltage"), "V")
    print()
    try:
        if a.both or a.turn:
            do_turn(cc, a.speed, a.turn or 1.0)
            if a.both:
                print()
                print(">>> 转角量完。给你 15 秒归位 / 在起点做标记，然后测前进距离")
                for i in range(15, 0, -1):
                    if i in (15, 10, 5, 3, 2, 1):
                        print("    %ds…" % i, flush=True)
                    time.sleep(1.0)
        if a.both or a.forward:
            do_forward(cc, a.speed, a.forward or 2.0)
    finally:
        print()
        try:
            cc.stop()
            cc.disconnect()
            print("  已停车并断开")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
