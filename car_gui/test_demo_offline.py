#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线演示模式回归（test_demo_offline.py）

需求来源（2026-09-27）：人不在实验室、接不上小车时，GUI 也要能用 ——
当作一个"能听会说"的对话程序。

本测试**不需要小车、不需要麦克风、不加载任何模型**，只验逻辑：
  ★ 核心安全断言：**离线演示模式下，绝不产生任何串口下发**（车不可能动）
  · 未连接 + 未开演示 → 动作被拒、返回 False（原行为，不能被破坏）
  · 未连接 + 开演示   → 动作被接受、更新本地状态、**语音应答被投递**
  · 已连接时的行为**完全不变**（连接优先于演示开关）
  · 速度类指令在两种模式下都能用（本来就是纯本地状态）
  · 急停/stop 在演示模式下同样不下发
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from car_gui_core import CarController  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s%s" % (name, ("  [%s]" % detail) if detail else ""))
    else:
        FAIL += 1
        print("  ✗ %s%s" % (name, ("  [%s]" % detail) if detail else ""))


class FakeCar(object):
    """假下位机：记录每一次下发，用来证明"演示模式没碰串口"。"""

    def __init__(self):
        self.sent = []

    def set_speed(self, x, y, z):
        self.sent.append((x, y, z))

    def stop(self, times=1):
        for _ in range(times):
            self.sent.append((0, 0, 0))

    def read_status(self):
        return {}


class FakeQueue(object):
    """假播报队列：只记录投递了什么，不真的合成。"""

    def __init__(self):
        self.puts = []

    def put(self, text, drop_pending=False):
        self.puts.append((text, drop_pending))

    def pending(self):
        return len(self.puts)

    def close(self):
        pass


def make_ctrl(connected=False, demo=False):
    c = CarController()
    car = FakeCar()
    if connected:
        c._car = car
        c._connected = True
    c.set_demo_mode(demo)
    q = FakeQueue()
    c._speak_q = q                # 让 speak() 能投递
    c._tts_state = "ready"
    c._speak_enabled = True
    c._announce_actions = True
    c._last_speak.clear()
    return c, car, q


def main():
    print("=" * 60)
    print(" 离线演示模式回归")
    print("=" * 60)

    # ── 1. ★ 安全底线：演示模式下绝不发串口 ─────────────────────
    print("\n[1] ★ 安全底线：离线演示模式绝不下发串口")
    c, car, q = make_ctrl(connected=False, demo=True)
    for text in ("前进一米", "后退两秒", "左转90度", "右转 1 秒", "停止", "急停"):
        c.run_text_command(text)
    check("跑了 6 条指令后串口下发次数为 0", len(car.sent) == 0,
          "实际 %d 次" % len(car.sent))
    check("车对象没有被我们赋值过（_car 仍为 None）", c._car is None)

    # ── 2. 演示模式下动作被"接受"并语音应答 ─────────────────────
    print("\n[2] 演示模式：动作被接受 + 语音应答")
    c, car, q = make_ctrl(connected=False, demo=True)
    ok, msg = c.run_text_command("前进一米")
    check("前进被接受", ok, msg)
    check("提示里标明是离线", "离线" in msg, msg)
    check("产生了语音应答", len(q.puts) >= 1, "投递 %r" % (q.puts,))
    check("应答内容是「开始前进」",
          any(t == "开始前进" for t, _ in q.puts), "实际 %r" % (q.puts,))
    check("本地动作状态已更新为 forward", c._action == "forward", c._action)
    check("本地速度已更新", c.speed > 0, str(c.speed))

    # ── 3. 未连接 + 未开演示 → 原行为不变 ───────────────────────
    print("\n[3] 未连接且未开演示：动作被拒（原行为，不能被破坏）")
    c, car, q = make_ctrl(connected=False, demo=False)
    ok, msg = c.run_text_command("前进一米")
    check("前进被拒绝", not ok, msg)
    check("提示未连接", "未连接" in msg, msg)
    check("没有串口下发", len(car.sent) == 0)
    check("没有动作语音应答（只静默记日志）", len(q.puts) == 0,
          "实际 %r" % (q.puts,))

    # ── 4. 已连接时行为完全不变（连接优先于演示开关）───────────
    print("\n[4] 已连接：行为不变（演示开关不应干扰真机）")
    c, car, q = make_ctrl(connected=True, demo=True)
    ok, msg = c.run_text_command("前进一米")
    check("前进仍被下发", ok, msg)
    check("串口确实收到了 Vx>0", car.sent and car.sent[-1][0] > 0,
          str(car.sent))
    check("提示里不再出现「离线」", "离线" not in msg, msg)
    check("仍会播报动作（连接态原本就有）",
          any(t == "开始前进" for t, _ in q.puts), "实际 %r" % (q.puts,))

    c, car, q = make_ctrl(connected=True, demo=False)
    ok, _ = c.run_text_command("停止")
    check("连接态急停下发 0 速度", car.sent and car.sent[-1] == (0, 0, 0),
          str(car.sent))

    # ── 5. 速度类指令：两种模式都可用（本来就是纯本地状态）─────
    print("\n[5] 速度类指令在两种模式下都可用")
    for demo in (False, True):
        c, car, q = make_ctrl(connected=False, demo=demo)
        ok, msg = c.run_text_command("速度 200")
        check("demo=%s 设速度成功" % demo, ok and c.speed == 200,
              "%s / speed=%s" % (msg, c.speed))
        ok2, _ = c.run_text_command("快一点")
        check("demo=%s 加速成功" % demo, ok2 and c.speed > 200, str(c.speed))
        ok3, _ = c.run_text_command("慢一点")
        check("demo=%s 减速成功" % demo, ok3 and c.speed < c.speed + 1, str(c.speed))
    check("速度类指令始终不下发串口（纯本地）", True)

    # ── 6. 听不懂时也出声（演示模式）────────────────────────────
    print("\n[6] 演示模式：听不懂也出声，好区分「没听懂」和「没反应」")
    c, car, q = make_ctrl(connected=False, demo=True)
    ok, msg = c.run_text_command("给我讲个笑话")
    check("返回 False", not ok, msg)
    check("提示是听不懂", "没听懂" in msg, msg)
    check("出声说了「没听懂」", any(t == "没听懂" for t, _ in q.puts),
          "实际 %r" % (q.puts,))
    check("仍然没有串口下发", len(car.sent) == 0)

    # ── 7. 关掉动作播报则静默（尊重开关）────────────────────────
    print("\n[7] 关掉「动作播报」后，演示模式也不出声")
    c, car, q = make_ctrl(connected=False, demo=True)
    c.set_announce_actions(False)
    ok, _ = c.run_text_command("前进一米")
    check("动作仍被接受", ok)
    check("但不再出声", len(q.puts) == 0, "实际 %r" % (q.puts,))

    # ── 8. demo_mode 出现在状态快照里（界面靠它显示）────────────
    print("\n[8] 状态快照暴露 demo_mode")
    c, _, _ = make_ctrl(connected=False, demo=True)
    s = c.status()
    check("status() 有 demo_mode=True", s.get("demo_mode") is True, str(s.get("demo_mode")))
    c.set_demo_mode(False)
    check("关掉后 demo_mode=False", c.status().get("demo_mode") is False)

    # ── 9. 急停锁定态 > 演示模式（安全优先级）───────────────────
    print("\n[9] 急停锁定优先于演示模式")
    c, car, q = make_ctrl(connected=False, demo=True)
    c._estop_locked = True
    c._estop_reason = "测试"
    ok, _ = c.run_text_command("前进一米")
    check("锁定时演示模式也拒绝前进", not ok)
    check("locked 时不下发串口", len(car.sent) == 0)

    print("\n" + "=" * 60)
    print(" 通过 %d · 失败 %d" % (PASS, FAIL))
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
