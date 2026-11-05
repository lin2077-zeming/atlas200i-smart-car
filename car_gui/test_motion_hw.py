#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_motion_hw.py —— 真机运动验证（需要：小车串口）

⚠️ **会让车真的动。** 请先把**车轮离地**（或确保周围空旷），再跑本脚本。

验什么
------
1. **闭环**：下发动作后，下位机回报的 Vx/Vy/Vz 是否跟着动、方向对不对
2. **量纲**：命令 200 mm/s，下位机回报的 Vx 是不是也≈200（验证"速度即真实 mm/s"）
3. **麦轮 Y 轴**：横向平移能不能走（只有麦轮/全向轮支持）
4. **急停切断持续运动** ★ 最重要的安全属性：
   先让车**持续**动起来，再触发急停，确认速度立刻归零
5. **方向标定**：打印左转/右转时 Vz 的符号，供你对照实际旋转方向
   （本项目代码用"z>0 右转"，而说明书标 Z+ 是逆时针/左转，两者相反 ——
     实机必须确认一次，不符就改 `car_gui_core.LEFT_RIGHT_SWAP`）

安全设计
--------
- 所有常规动作都带 `duration`，**由后台 Timer 自动停车** ——
  就算脚本中途崩了，车也会自己停。
- 只有"急停切断"那一项用持续运动（duration=None），
  因为要验的正是这个；外面用 try/finally 兜底停车。
- 每项之间都会 `stop()` 并等回稳。

用法
----
    bash run_py.sh test_motion_hw.py                    # 默认速度 200，每项 1.2 秒
    bash run_py.sh test_motion_hw.py --speed 150
    bash run_py.sh test_motion_hw.py --skip-cut         # 跳过"持续运动+急停切断"
    bash run_py.sh test_motion_hw.py --port /dev/ttyCAR
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import car_gui_core as core                             # noqa: E402

PASS = 0
FAIL = 0
WARN = 0


def check(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("   ✓ %s" % label)
    else:
        FAIL += 1
        print("   ❌ %s%s" % (label, ("   ← " + str(extra)) if extra else ""))


def note(label, extra=""):
    global WARN
    WARN += 1
    print("   ⚠️  %s%s" % (label, ("   " + str(extra)) if extra else ""))


def sample(ctrl, seconds, period=0.04):
    """在一段时间内密集采样下位机回报。返回 [(t, vx, vy, vz, volt), ...]"""
    out = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        s = ctrl.status()
        if s["have_feedback"]:
            out.append((time.time() - t0, s["vx"], s["vy"], s["vz"],
                        s["voltage"]))
        time.sleep(period)
    return out


def run_action(ctrl, action, speed, dur, settle=0.5):
    """
    执行一个动作并采样。

    **带 duration** → 后台 Timer 自动停车（安全兜底）。
    返回 (动作期间的采样, 静止后的采样)。
    """
    during = []
    ctrl.move(action, speed=speed, duration=dur)
    during = sample(ctrl, dur)
    # 等定时停车生效 + 车回稳
    during += sample(ctrl, 0.25)
    time.sleep(settle)
    after = sample(ctrl, 0.35)
    return during, after


def peak(vals):
    return max((abs(v) for v in vals), default=0)


def main():
    ap = argparse.ArgumentParser(description="真机运动验证（会让车动！）")
    ap.add_argument("--port", default=None, help="串口，默认自动探测")
    ap.add_argument("--baud", type=int, default=core.DEFAULT_BAUD)
    ap.add_argument("--speed", type=int, default=200, help="测试速度 mm/s")
    ap.add_argument("--dur", type=float, default=3.0,
                    help="每项持续秒数。**默认 3 秒**：实测电机起速要约 2 秒，"
                         "窗口太短只会截到爬升段（1.2s 时 Vx 只有命令的 0.3 倍）")
    ap.add_argument("--skip-cut", action="store_true",
                    help="跳过「持续运动 + 急停切断」那一项")
    args = ap.parse_args()

    print("=" * 74)
    print("  真机运动验证　⚠️ 车轮应当已离地")
    print("=" * 74)
    print("  速度 %d mm/s　每项 %.1f 秒" % (args.speed, args.dur))

    ctrl = core.CarController()
    ok, msg = ctrl.connect(args.port, args.baud)
    print("\n① 连接串口：%s %s" % ("✅" if ok else "❌", msg))
    check(ok, "串口连接成功")
    if not ok:
        return 1
    print("   端口：%s" % ctrl.port)

    results = {}
    try:
        # ── 基线：必须静止 ────────────────────────────────────────────
        print("\n② 基线（车应静止）…")
        base = sample(ctrl, 1.0)
        check(bool(base), "收到状态帧")
        if base:
            bvx = peak([b[1] for b in base])
            bvz = peak([b[3] for b in base])
            print("   电压 %.2fV   峰值 |Vx|=%d  |Vz|=%d"
                  % (base[-1][4], bvx, bvz))
            check(bvx == 0 and bvz == 0, "初始确实静止（Vx=Vz=0）",
                  "Vx=%d Vz=%d" % (bvx, bvz))
            check(base[-1][4] > core.VOLT_LOCK,
                  "电压高于锁死线 %.1fV" % core.VOLT_LOCK, base[-1][4])

        # ── 各动作 ───────────────────────────────────────────────────
        # 最后一列 optional=True 表示"这是能力探测，不是必须支持"：
        # 说明书原文「y>0 左移 …（**仅麦轮/全向轮支持；四驱、差速、阿克曼、
        # 履带车不支持**）」。本车固件按"四驱"驱动，实测 vy 恒为 0 →
        # **Y 轴（横向平移）不可用**，属于能力边界而非代码 bug。
        CASES = [
            ("forward",      1, "前进",   "vx", +1, False),
            ("backward",     1, "后退",   "vx", -1, False),
            ("strafe_left",  2, "左移",   "vy", +1, True),
            ("strafe_right", 2, "右移",   "vy", -1, True),
            ("turn_left",    3, "左转",   "vz", -1, False),
            ("turn_right",   3, "右转",   "vz", +1, False),
        ]

        print("\n③ 逐个动作验证（命令 → 下位机回报）")
        print("   %-6s %-22s %-22s %s" % ("动作", "命令速度", "回报峰值", "判定"))
        print("   " + "-" * 68)
        for act, _k, label, axis, want_sign, optional in CASES:
            during, after = run_action(ctrl, act, args.speed, args.dur)
            idx = {"vx": 1, "vy": 2, "vz": 3}[axis]
            vals = [d[idx] for d in during]
            pk = peak(vals)
            # 取"与期望方向一致的那个极值"，看它有多大
            signed = max(vals) if want_sign > 0 else min(vals)
            results[act] = {"during": during, "after": after,
                            "peak": pk, "signed": signed, "axis": axis,
                            "want": want_sign, "label": label}

            good_dir = (pk > 0) and (
                (signed > 0) if want_sign > 0 else (signed < 0))
            tail_vx = peak([a[1] for a in after])
            tail_vz = peak([a[3] for a in after])
            stopped = (tail_vx == 0 and tail_vz == 0)

            if optional and pk == 0:
                print("   %-6s %-22s %-22s ⚠️  不支持（%s 恒为 0）"
                      % (label, "%s=%+d" % (axis, args.speed * want_sign),
                         "%s=+0" % axis, axis))
                note("%s：下位机不上报 %s —— 固件按四驱驱动，"
                     "说明书明确「四驱不支持 Y 轴」，属能力边界"
                     % (label, axis))
            else:
                mark = "✅" if (good_dir and stopped) else "❌"
                print("   %-6s %-22s %-22s %s%s"
                      % (label,
                         "%s=%+d" % (axis, args.speed * want_sign),
                         "%s=%+d" % (axis, signed),
                         mark,
                         "" if stopped else "  （停稳后仍有速度！）"))
                check(good_dir, "%s：%s 方向正确（%s 符号符合预期）"
                      % (label, axis, axis), "%s=%+d" % (axis, signed))
            check(stopped, "%s：定时停车后确实静止" % label,
                  "|Vx|=%d |Vz|=%d" % (tail_vx, tail_vz))

        # ── 量纲检查 ─────────────────────────────────────────────────
        print("\n④ 量纲检查（本项**只验「是不是同一量纲」**，不考控制环）")
        print("   判据：回报值应落在命令的 0.4~2.5 倍之间。")
        print("   这能排除差 10/100 倍的量纲错误（比如把 PWM 值或 0.1mm/s 当成了 mm/s）。")
        for act in ("forward", "backward"):
            r = results.get(act)
            if not r:
                continue
            got = peak([d[1] for d in r["during"]])
            ratio = got / float(args.speed) if args.speed else 0
            print("   %s：命令 %d → 回报峰值 %d（%.2f 倍）"
                  % (r["label"], args.speed, got, ratio))
            check(0.4 <= ratio <= 2.5,
                  "%s：回报速度与命令同一量纲（0.4~2.5 倍）" % r["label"],
                  "%.2f 倍" % ratio)

        # ── 方向标定提示 ─────────────────────────────────────────────
        print("\n⑤ 方向标定（需要你用眼睛确认）")
        lt = results.get("turn_left")
        rt = results.get("turn_right")
        if lt and rt:
            print("   代码下发的「左转」→ 下位机回报 Vz=%+d" % lt["signed"])
            print("   代码下发的「右转」→ 下位机回报 Vz=%+d" % rt["signed"])
            print()
            print("   ⚠️ 请对照实际观察：按「左转」时小车是**逆时针（左）**转吗？")
            print("      若实际是向右转 → 把 car_gui_core.py 里")
            print("        LEFT_RIGHT_SWAP = False   改成   True")
            note("转向方向需人工目视确认（代码里 LEFT_RIGHT_SWAP 可一键翻转）")

        # ── ★ 急停切断持续运动 ───────────────────────────────────────
        print("\n⑥ ★ 急停能否切断**持续运动**（最重要的一条安全属性）")
        if args.skip_cut:
            print("   （--skip-cut，跳过）")
        else:
            # 持续运动（duration=None）—— 用 try/finally 兜底
            ctrl.move("forward", speed=args.speed, duration=None)
            moving = sample(ctrl, 0.7)
            mvx = peak([m[1] for m in moving])
            check(mvx > 0, "已进入持续运动（Vx 非零）", "|Vx|=%d" % mvx)

            t_lat = time.time()
            ctrl.trigger_emergency(reason="运动测试", announce=False)
            lat = time.time() - t_lat
            after_cut = sample(ctrl, 1.3)
            cut_vx = peak([a[1] for a in after_cut])
            # ⚠️ 判据说明：本板**轮子离地空转有惯性**，切断动力后不会瞬间停转
            #    （实测 1.3 秒内峰值还有 ~49）。那是物理，不是"没切断"。
            #    真正要验的是**速度在衰减、并且已经掉到很低**。
            tail = [a for a in after_cut if a[0] > 0.9]
            tail_vx = peak([a[1] for a in tail])
            tail_vz = peak([a[3] for a in tail])
            print("   急停调用耗时 %.0f ms" % (lat * 1000))
            print("   切断后 1.3s 全程峰值 |Vx|=%d（惯性滑行）；"
                  "**最后 0.4s** 峰值 |Vx|=%d |Vz|=%d"
                  % (cut_vx, tail_vx, tail_vz))
            check(lat < 0.5, "急停调用是短路径（<500ms）", "%.0f ms" % (lat * 1000))
            limit = max(20, int(mvx * 0.25))
            check(tail_vx <= limit,
                  "**急停后速度已衰减到接近 0**（持续运动被切断，残留 %d ≤ %d）"
                  % (tail_vx, limit),
                  "终值 |Vx|=%d" % tail_vx)
            check(ctrl.estop_locked, "并进入了急停锁定态")

            # 锁定态下不响应任何动作
            refused = ctrl.move("forward", speed=args.speed, duration=0.3)
            after_refuse = sample(ctrl, 0.4)
            rf_vx = peak([a[1] for a in after_refuse])
            check(refused is False and rf_vx == 0,
                  "锁定态下再次前进被拒绝，车保持静止",
                  "|Vx|=%d" % rf_vx)
            ctrl._cancel_timer()

            ctrl.release_estop(announce=False)
            check(not ctrl.estop_locked, "已解除锁定")

    except KeyboardInterrupt:
        print("\n   中断")
    finally:
        # ── 收尾：一定要停 ───────────────────────────────────────────
        print("\n⑦ 收尾：停车 + 断开")
        try:
            ctrl._cancel_timer()
            ctrl.stop()
            time.sleep(0.3)
            s = ctrl.status()
            print("   最终状态：Vx=%d Vz=%d 电压 %.2fV"
                  % (s["vx"], s["vz"], s["voltage"]))
            check(s["vx"] == 0 and s["vz"] == 0, "收尾时车已完全停止")
        except Exception as e:
            print("   停车异常：%s" % e)
        ctrl.disconnect()

    print("\n" + "=" * 74)
    print("  通过 %d · 失败 %d · 待人工确认 %d" % (PASS, FAIL, WARN))
    print("=" * 74)
    if FAIL:
        print("\n⚠️  有失败项 —— 运动链路是安全相关，必须查清再上车。")
    else:
        print("\n✅ 运动链路全通（方向仍需你目视确认一次）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
