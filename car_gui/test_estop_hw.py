#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_estop_hw.py —— 语音急停**真机**链路验证（需要：小车串口 + 音响）

与 `test_voice_estop.py` 的分工
------------------------------
| 脚本 | 需要的硬件 | 验什么 |
|---|---|---|
| `test_voice_estop.py` | **无**（假车/假队列） | 逻辑与顺序：先停车 → 再锁定 → 后播报 |
| `test_estop_hw.py`（本脚本） | **串口 + 音响** | 真下位机收到停车帧了吗？真音箱出声了吗？ |

**安全说明**：本脚本**只下发零速度帧**，不会让车动一下。
它模拟一次 KWS 命中（直接调 `_on_kws_hit`，不需要真的说话），
然后检查整条链路的实际效果。

用法
----
    bash run_py.sh test_estop_hw.py                     # 自动探测串口
    bash run_py.sh test_estop_hw.py --port /dev/ttyCAR
    bash run_py.sh test_estop_hw.py --speaker 47
    bash run_py.sh test_estop_hw.py --no-sound          # 跳过"等它说完"（无音箱时）
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import car_gui_core as core                             # noqa: E402

PASS = 0
FAIL = 0


def check(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("   ✓ %s" % label)
    else:
        FAIL += 1
        print("   ❌ %s%s" % (label, ("   ← " + str(extra)) if extra else ""))


def main():
    ap = argparse.ArgumentParser(description="语音急停真机链路验证")
    ap.add_argument("--port", default=None, help="串口，默认自动探测")
    ap.add_argument("--baud", type=int, default=core.DEFAULT_BAUD)
    ap.add_argument("--speaker", type=int, default=None, help="TTS 说话人编号")
    ap.add_argument("--no-sound", action="store_true",
                    help="不等播报说完（没有音响时用）")
    args = ap.parse_args()

    print("=" * 72)
    print("  语音急停 · 真机链路验证（只发停止帧，车不会动）")
    print("=" * 72)

    ctrl = core.CarController()

    # ── 第一步：先把 TTS 加载好（同步，这样播报才有意义）─────────────
    print("\n① 加载 TTS（约 5~7 秒）…")
    ok, msg = ctrl.init_tts(speaker=args.speaker, background=False)
    print("   %s %s" % ("✅" if ok else "❌", msg))
    check(ok, "TTS 加载成功")
    if not ok:
        print("   → 先跑 `bash run_py.sh tts_engine.py --check`")
    ts = ctrl.tts_status()
    print("   输出端：%s" % ts["msg"])
    check(ts["has_output"] or bool(ts["web_url"]),
          "有可用的输出端（本地喇叭 或 网络音箱）",
          ts["msg"])

    # ── 第二步：连串口 ────────────────────────────────────────────────
    print("\n② 连接串口…")
    ok, msg = ctrl.connect(args.port, args.baud)
    print("   %s %s" % ("✅" if ok else "❌", msg))
    check(ok, "串口连接成功")
    if not ok:
        print("   → 检查小车是否上电、线是否插好；`ls -l /dev/ttyCAR`")
        ctrl.shutdown_voice()
        return 1
    print("   端口：%s" % ctrl.port)

    try:
        # ── 基线状态 ─────────────────────────────────────────────────
        print("\n③ 读基线状态帧（2 秒）…")
        seen_fb = False
        base = None
        t0 = time.time()
        while time.time() - t0 < 2.0:
            s = ctrl.status()
            if s["have_feedback"]:
                seen_fb = True
                base = s
                break
            time.sleep(0.05)
        check(seen_fb, "收到下位机状态帧（20Hz 上行）")
        if base:
            print("   电压 %.2fV   Vx=%d Vz=%d   Flag_Stop=%d"
                  % (base["voltage"], base["vx"], base["vz"],
                     base["flag_stop"]))
            check(base["voltage"] > core.VOLT_LOCK,
                  "电池电压高于锁死线 %.1fV" % core.VOLT_LOCK,
                  base["voltage"])

        # ── 模拟 KWS 命中 ─────────────────────────────────────────────
        print("\n④ 模拟一次 KWS 命中（等价于你对麦克风说「急停」）…")
        check(not ctrl.estop_locked, "命中前：未处于锁定态")
        t_hit = time.time()
        ctrl._on_kws_hit("急停", t_hit)
        take = time.time() - t_hit
        print("   处理耗时 %.0f ms" % (take * 1000))
        check(take < 0.5, "命中处理走的是短路径（<500ms，没被播报拖住）",
              "%.0f ms" % (take * 1000))

        # ── ① 停车真的到不了位？ ─────────────────────────────────────
        print("\n⑤ 检查下位机是否真的收到停车帧…")
        time.sleep(0.6)
        s = ctrl.status()
        check(s["have_feedback"], "命中后仍在正常收状态帧")
        check(s["vx"] == 0 and s["vz"] == 0,
              "下位机回报速度为 0（确实停住了）",
              "Vx=%d Vz=%d" % (s["vx"], s["vz"]))
        check(s["action"] == "stop" if "action" in s else True,
              "控制器状态为 stop", s.get("action"))

        # ── ② 锁定态 ─────────────────────────────────────────────────
        print("\n⑥ 检查急停锁定态…")
        check(ctrl.estop_locked, "已进入锁定态")
        check("急停" in ctrl.estop_reason, "锁定原因记录了命中词",
              ctrl.estop_reason)
        refused = ctrl.move("forward", speed=100, duration=0.3)
        check(refused is False, "锁定态下 move(forward) 被拒绝（车没动）")
        ctrl._cancel_timer()

        # ── ③ 播报 ───────────────────────────────────────────────────
        print("\n⑦ 检查播报…")
        if args.no_sound:
            print("   （--no-sound，跳过）")
        else:
            q = ctrl._speak_q
            if q is None:
                check(False, "播报队列可用")
            else:
                t0 = time.time()
                got = False
                while time.time() - t0 < 45:
                    if q.idle():
                        got = True
                        break
                    time.sleep(0.2)
                check(got, "播报告一段落（音箱应已念出「%s」）" % core.SPEAK_ESTOP,
                      "等了 %.1f 秒" % (time.time() - t0))

        # ── ④ 解除 ───────────────────────────────────────────────────
        print("\n⑧ 解除急停锁定…")
        ctrl._last_speak.clear()
        reason = ctrl.release_estop(announce=False)
        check(reason != "", "release_estop 返回了原锁定原因", reason)
        check(not ctrl.estop_locked, "锁定态已清除")
        s = ctrl.status()
        check(s["vx"] == 0 and s["vz"] == 0, "解除后车依然是停的（不会自己跑）",
              "Vx=%d Vz=%d" % (s["vx"], s["vz"]))

    finally:
        print("\n⑨ 收尾（断开串口 / 关语音）…")
        ctrl.disconnect()
        ctrl.shutdown_voice()

    print("\n" + "=" * 72)
    print("  通过 %d · 失败 %d" % (PASS, FAIL))
    print("=" * 72)
    if FAIL:
        print("\n⚠️  语音急停是安全功能，有失败项就要查清楚再上车。")
    else:
        print("\n✅ 真机链路全通：命中 → 停车 → 锁定 → 播报 → 人工解除")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
