#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_offline.py —— 不接硬件、不接小车，纯逻辑自测

用一个「假串口」替换真实的 CarSerial，验证：
  · 动作 → 三轴速度的映射是否正确
  · 定时停车（duration）是否按时生效
  · 急停、断连、文本指令是否走通
  · 状态快照与低压/急停标志判断是否正确

在项目根目录执行：
    python3 car_gui/selftest_offline.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from car_gui_core import CarController, parse_text_command  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("[PASS]" if cond else "[FAIL]", name, ("  " + extra) if extra else ""))


class FakeCar(object):
    """记录所有下发过的指令；可伪造回传状态帧。"""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.voltage = 12.30
        self.flag_stop = 0

    def set_speed(self, x=0, y=0, z=0):
        self.sent.append((int(x), int(y), int(z)))

    def get_feedback(self):
        vx, vy, vz = self.sent[-1] if self.sent else (0, 0, 0)
        return {
            "flag_stop": self.flag_stop,
            "vx": vx, "vy": vy, "vz": vz,
            "accel_x": 1671, "accel_y": 0, "accel_z": 0,
            "gyro_x": 0, "gyro_y": 0, "gyro_z": 1000,
            "voltage": self.voltage,
        }

    def close(self):
        self.closed = True

    def last(self):
        return self.sent[-1] if self.sent else None


def make_ctrl(voltage=12.30, flag_stop=0):
    c = CarController()
    fake = FakeCar()
    fake.voltage = voltage
    fake.flag_stop = flag_stop
    # 绕过真实 connect()，直接注入假串口
    c._car = fake
    c._connected = True
    c._port = "/dev/FAKE0"
    c._speed = 300
    return c, fake


def main():
    print("=" * 62)
    print(" 小车 GUI 离线自测（不使用真实串口）")
    print("=" * 62)

    # ── 1. 动作 → 三轴速度映射 ──────────────────────────────────────
    print("\n[1] 动作映射")
    # ⚠️ 方向约定（2026-09-27 实机目视确认）：
    #    Z 正 = 逆时针 = **左转**，Z 负 = 顺时针 = 右转。
    #    本文件曾长期写成"turn_left=(0,0,-300)"（按 car_serial.py 的旧注释），
    #    09-27 改 ACTION_XYZ 时**漏改了这个文件** → 板子上一直报 3 项失败，
    #    而记忆里还留着"全部通过"的旧结论（假绿）。现按实机结论改正。
    expect = {
        "forward":      (300, 0, 0),
        "backward":     (-300, 0, 0),
        "turn_left":    (0, 0, 300),    # Z+ = 逆时针 = 左转
        "turn_right":   (0, 0, -300),   # Z- = 顺时针 = 右转
        "strafe_left":  (0, 300, 0),
        "strafe_right": (0, -300, 0),
        "stop":         (0, 0, 0),
    }
    for act, want in expect.items():
        c, fake = make_ctrl()
        c.move(act)
        got = fake.last()
        check("%-13s -> %s" % (act, want), got == want, "实际 %s" % (got,))

    # ── 2. 速度变更 ────────────────────────────────────────────────
    print("\n[2] 速度调节")
    c, fake = make_ctrl()
    check("默认速度 300", c.speed == 300)
    check("上界钳位到 500", c.set_speed_value(9999) == 500)
    check("下界钳位到 50", c.set_speed_value(1) == 50)
    check("加速 +50", c.speed_up() == 100)
    check("减速 -50", c.speed_down() == 50)
    c.move("forward", 200)
    check("指定速度生效", fake.last() == (200, 0, 0))

    # ── 3. 定时停车 ────────────────────────────────────────────────
    print("\n[3] 定时停车（duration）")
    c, fake = make_ctrl()
    c.move("forward", 300, 0.5)
    check("立刻下发前进", fake.last() == (300, 0, 0))
    time.sleep(0.25)
    check("中途还在走（未提前停）", fake.last() != (0, 0, 0), "t=0.25s")
    time.sleep(0.45)
    check("到点自动停止", fake.last() == (0, 0, 0), "t=0.70s")

    # ── 4. 新指令取消旧定时器 ──────────────────────────────────────
    print("\n[4] 新指令抢占旧定时")
    c, fake = make_ctrl()
    c.move("forward", 300, 0.4)
    time.sleep(0.1)
    c.move("turn_right", 300)          # 持续模式，不应被旧的 0.4s 定时器停掉
    time.sleep(0.5)
    # turn_right = Z 负（顺时针）
    check("旧定时器已被取消", fake.last() == (0, 0, -300), "实际 %s" % (fake.last(),))

    # ── 5. 急停 ────────────────────────────────────────────────────
    print("\n[5] 急停")
    c, fake = make_ctrl()
    c.move("forward", 400)
    c.emergency_stop()
    check("急停后速度为 0", fake.last() == (0, 0, 0))
    check("急停连发多次", len([s for s in fake.sent if s == (0, 0, 0)]) >= 5,
          "共 %d 次" % len(fake.sent))

    # ── 6. 文本指令 ────────────────────────────────────────────────
    print("\n[6] 文本指令执行")
    c, fake = make_ctrl()
    ok, _ = c.run_text_command("速度 200")
    check("设速度", c.speed == 200 and ok)
    ok, _ = c.run_text_command("前进一米")
    check("前进一米 -> 200mm/s 立即走", fake.last() == (200, 0, 0))
    time.sleep(0.2)
    c.run_text_command("急停")
    check("文本急停", fake.last() == (0, 0, 0))
    ok, msg = c.run_text_command("给我讲个笑话")
    check("听不懂不执行", (not ok), msg)

    # ── 7. 状态快照 ────────────────────────────────────────────────
    print("\n[7] 状态快照")
    c, fake = make_ctrl(voltage=12.30)
    c.move("forward", 250)
    s = c.status()
    check("有回传", s["have_feedback"])
    check("Vx 跟随指令", s["vx"] == 250)
    check("电压正常判定", s["volt_level"] == "ok", "%.2fV" % s["voltage"])
    check("加速度换算", abs(s["accel_ms2"][0] - 1671 / 1671.84) < 1e-6)
    check("角速度换算", abs(s["gyro_rads"][2] - 1000 * 0.00026644) < 1e-9)

    c, fake = make_ctrl(voltage=10.5)
    check("低压告警判定", c.status()["volt_level"] == "warn")
    c, fake = make_ctrl(voltage=9.6)
    check("低压锁死判定", c.status()["volt_level"] == "danger")

    c, fake = make_ctrl(flag_stop=1)
    check("急停标志回传", c.status()["flag_stop"] == 1)

    # ── 8. 断连 ────────────────────────────────────────────────────
    print("\n[8] 断开连接")
    c, fake = make_ctrl()
    c.move("forward")
    c.disconnect()
    check("串口已关闭", fake.closed)
    check("状态变为未连接", not c.status()["connected"])
    ok = c.move("forward")            # 断连后下发应被拒绝
    check("断连后拒绝下发", ok is False)

    # ── 9. 未连接时不应崩 ──────────────────────────────────────────
    print("\n[9] 健壮性")
    c = CarController()
    try:
        c.move("forward"); c.stop(); c.emergency_stop()
        c.run_text_command("前进"); c.status(); c.disconnect()
        check("未连接时所有接口都不抛异常", True)
    except Exception as e:
        check("未连接时所有接口都不抛异常", False, repr(e))

    # ── 10. 语音识别纯逻辑（不需要麦克风、不需要模型）─────────────────
    print("\n[10] 语音识别纯逻辑")
    try:
        import asr_engine as A
        has_asr = True
    except Exception as e:
        has_asr = False
        print("  [SKIP] 无法导入 asr_engine：%s" % e)

    if has_asr:
        r1 = A.clean_text("<|zh|><|NEUTRAL|><|Speech|><|woitn|>前进一米")
        check("clean_text 去掉 <|...|> 标签", r1 == "前进一米", r1)
        check("clean_text 保留正常文本",
              A.clean_text("  开放时间早上9点。 ") == "开放时间早上9点。")
        check("clean_text 空输入安全", A.clean_text("") == "" and A.clean_text(None) == "")

        import shutil
        import tempfile
        d = tempfile.mkdtemp()
        m, t = A.find_model_files(d)
        check("空目录找不到模型", m is None and t is None)
        for fn in ("model.int8.onnx", "tokens.txt"):
            open(os.path.join(d, fn), "w").close()
        m, t = A.find_model_files(d)
        check("能找到 model.int8.onnx", bool(m) and m.endswith("model.int8.onnx"))
        check("能找到 tokens.txt", bool(t) and t.endswith("tokens.txt"))
        shutil.rmtree(d, ignore_errors=True)

        st = A.dependency_status()
        check("dependency_status 返回三项",
              all(k in st for k in ("numpy", "sounddevice", "sherpa_onnx")))

        if A.HAS_NUMPY:
            import numpy as np

            y = A._resample_to_16k(np.zeros(44100, dtype=np.float32), 44100)
            check("重采样 44100→16000 长度正确", abs(len(y) - 16000) <= 2, "len=%d" % len(y))
            check("RMS 静音为 0", abs(A._rms(np.zeros(1600, dtype=np.float32))) < 1e-9)
            check("RMS 全幅约为 1",
                  abs(A._rms(np.ones(1600, dtype=np.float32)) - 1.0) < 1e-6)

            # 能量端点检测：静音 → 说话 → 静音，应依次出现 idle/speech/end
            det = A.EnergyEndpointDetector(frame_ms=30)
            states = []
            for _ in range(15):                 # 0.45s 静音
                states.append(det.feed(0.001))
            for _ in range(20):                 # 0.60s 说话
                states.append(det.feed(0.15))
            end_at = None
            for i in range(80):                 # 最长 2.4s 静音
                s = det.feed(0.001)
                states.append(s)
                if s == "end":
                    end_at = i
                    break
            check("静音期判为 idle", states[0] == "idle")
            check("说话期能判出 speech", "speech" in states)
            check("说完静音后能判出 end", end_at is not None,
                  "静音 %.2fs 后结束" % ((end_at or 0) * 0.03))

            # Recorder.pending 的游标语义（自动聆听靠它做增量取音）
            r = A.Recorder()
            r.samplerate = 16000
            r._chunks = [np.ones(100, dtype=np.float32), np.ones(50, dtype=np.float32)]
            cur, first = r.pending(0)
            check("pending 首次返回全部样本", cur == 2 and len(first) == 150,
                  "cur=%d len=%d" % (cur, len(first)))
            r._chunks.append(np.ones(30, dtype=np.float32))
            cur, more = r.pending(cur)
            check("pending 增量只返回新增", cur == 3 and len(more) == 30,
                  "cur=%d len=%d" % (cur, len(more)))
            cur, empty = r.pending(cur)
            check("pending 无新增时返回空", len(empty) == 0)
            check("total_samples 统计正确", r.total_samples() == 180)
        else:
            print("  [SKIP] 没有 numpy，跳过重采样/端点检测测试")

    # ── 汇总 ───────────────────────────────────────────────────────
    print("\n" + "=" * 62)
    print(" 通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print(" 失败清单：")
        for f in FAIL:
            print("   -", f)
        return 1
    print(" 全部通过。控制逻辑没问题，可以接硬件了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
