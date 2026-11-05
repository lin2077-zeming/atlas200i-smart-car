#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真车验证：走**生产路径**跑一句距离指令，看实际距离对不对。

用法：bash run_py.sh verify_cmd.py --text "前进一米" --expect-mm 1000
"""
import argparse
import sys
import time

sys.path.insert(0, "/home/HwHiAiUser/car_gui")
import car_gui_core as C                                    # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="要验证的指令文本")
    ap.add_argument("--expect-mm", type=float, default=1000.0,
                    help="期望走的距离(mm)，仅用于提示")
    a = ap.parse_args()

    cc = C.CarController()
    cc._announce_actions = False          # 静音，隔离运动计时
    print("=" * 66)
    print(" 真车验证：%r" % a.text)
    print("=" * 66)
    ok, msg = cc.connect()
    print("  连接:", ok, msg)
    if not ok:
        return 1
    print("  电压:", cc.status().get("voltage"), "V")
    print("  当前速度(命令值):", cc.speed, "→ 实测约 %d mm/s"
          % C.mmps_for_display(cc.speed))

    parsed, label = C.parse_text_command(a.text, cc.speed)
    print()
    print("  解析结果:", parsed, "  标签:", label)
    if not parsed:
        print("  ❌ 没解析出指令")
        cc.disconnect()
        return 1
    dur = parsed.get("duration")
    spd = parsed.get("speed")
    if parsed.get("action") in ("forward", "backward") and dur:
        est = C.cmd_to_mmps(spd) * max(0.0, dur - C.SPEED_RAMP_SEC / 2.0)
        print("  预期：命令值 %d → 速度 %.0f mm/s，走 %.2fs" % (spd, C.cmd_to_mmps(spd), dur))
        print("  **预估实际距离 = %.0f mm（目标 %.0f mm）**" % (est, a.expect_mm))
    else:
        print("  时长:", dur, " 速度:", spd)
    # 预估这段指令总共要跑多久（**多步计划要累加各步**，否则 finally 会中途刹车）
    plan = C.parse_plan(a.text, cc.speed)
    if plan:
        wait = sum((s.get("duration") or 0) for s in plan)
        wait += C.PLAN_STEP_SETTLE_SEC * (len(plan) - 1) + 2.0
    else:
        wait = (parsed.get("duration") or 0) + 1.5

    print()
    print("  📏 请在车头正前方放个标记，跑完量实际距离")
    for i in (5, 4, 3, 2, 1):
        print("     %d…" % i, flush=True)
        time.sleep(1.0)

    try:
        print("   ▶ 执行中…", flush=True)
        t0 = time.time()
        ok2, msg2 = cc.run_text_command(a.text)
        # 🔴 `run_text_command()` 对运动**不阻塞**（内部用 Timer 定时停车），
        #    所以这里必须自己等够时长再收尾 —— 否则 finally 里的 stop()
        #    会刚启动就刹车，车"没动"（第一版脚本就是这么错的）。
        print("   等待 %.2fs（%s）…"
              % (wait, ("多步 %d 步" % len(plan)) if plan else
                 ("单步 %.2fs" % (parsed.get("duration") or 0))), flush=True)
        time.sleep(wait)
        dt = time.time() - t0
        print("   ■ 完成：ok=%s msg=%r  总耗时 %.2fs" % (ok2, msg2, dt))
    finally:
        try:
            cc.stop()
        except Exception:
            pass
        for _ in range(5):
            try:
                cc._write_speed(0, 0, 0)
            except Exception:
                pass
            time.sleep(0.02)
        try:
            cc.disconnect()
        except Exception:
            pass
        print()
        print("  👉 现在量：车头实际走了多少 mm？")
        print("     期望 %.0f mm；若差得多，把实测值告诉我，我修系数。" % a.expect_mm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
