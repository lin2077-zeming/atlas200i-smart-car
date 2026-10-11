#!/usr/bin/env python3
"""
端到端闭环测试：ASR 识别文本 → 指令解析 → 串口下发
用 pty 伪造下位机，验证整条链路。
"""
import os
import pty
import struct
import sys
import threading
import time

sys.path.insert(0, '/home/HwHiAiUser/car_gui')

FRAME_HEAD, FRAME_TAIL = 0x7B, 0x7D


def bcc(d):
    v = 0
    for b in d:
        v ^= b
    return v & 0xFF


def status_frame(vx=0, vy=0, vz=0, volt=12300):
    b = bytearray(24)
    b[0] = FRAME_HEAD
    struct.pack_into('>h', b, 2, int(vx))
    struct.pack_into('>h', b, 4, int(vy))
    struct.pack_into('>h', b, 6, int(vz))
    struct.pack_into('>H', b, 20, int(volt))
    b[22] = bcc(b[0:22])
    b[23] = FRAME_TAIL
    return bytes(b)


class Slave(threading.Thread):
    def __init__(self, fd):
        super().__init__(daemon=True)
        self.fd = fd
        self.stop_evt = threading.Event()
        self.cmds = []
        self._buf = bytearray()

    def run(self):
        import select
        last = 0.0
        vx = vy = vz = 0
        while not self.stop_evt.is_set():
            now = time.time()
            if now - last >= 0.05:
                last = now
                try:
                    os.write(self.fd, status_frame(vx, vy, vz))
                except OSError:
                    break
            r, _, _ = select.select([self.fd], [], [], 0.01)
            if r:
                try:
                    self._buf.extend(os.read(self.fd, 512))
                except OSError:
                    break
            while len(self._buf) >= 11:
                if self._buf[0] != FRAME_HEAD or self._buf[10] != FRAME_TAIL:
                    self._buf.pop(0)
                    continue
                f = bytes(self._buf[:11])
                del self._buf[:11]
                self.cmds.append((
                    struct.unpack('>h', f[3:5])[0],
                    struct.unpack('>h', f[5:7])[0],
                    struct.unpack('>h', f[7:9])[0],
                ))
                vx, vy, vz = self.cmds[-1]


def main():
    master_fd, slave_fd = pty.openpty()
    name = os.ttyname(slave_fd)
    slave = Slave(master_fd)
    slave.start()

    import car_gui_core as C

    ctrl = C.CarController()
    ok, msg = ctrl.connect(name)
    print(f"[连接] ok={ok} msg={msg}")
    time.sleep(0.3)

    # 模拟 ASR 识别出来的文本（含真实的识别误差场景）
    # ⚠️ 转向方向以**实机目视标定**为准（2026-09-27 船长确认）：
    #    Z 正 = 逆时针 = 左转，Z 负 = 顺时针 = 右转。
    #    本表曾长期写成相反值（照抄 car_serial.py 的旧注释），改 ACTION_XYZ 时
    #    漏改这里 → 一直是 2 项失败。现按实机结论改正。
    cases = [
        ("前进",              'x>0',        "持续前进"),
        ("前进一米",           'x>0',        "定距前进"),
        ("后退两秒",           'x<0',        "定时后退"),
        ("左转90度",           'z>0',        "定角左转"),
        ("右转",              'z<0',        "持续右转"),
        ("左移",              'y>0',        "麦轮左移"),
        ("右移",              'y<0',        "麦轮右移"),
        ("停",                'zero',       "急停"),
        ("速度调到两百",        'spd=200',    "设速"),
        ("快一点",            'spd_up',     "加速"),
        ("慢一点",            'spd_dn',     "减速"),
        ("给我讲个笑话",        None,         "未识别(应拒绝)"),
    ]

    print("\n" + "=" * 74)
    print(f"{'语音文本':<14}{'期望':<10}{'实际下发':<22}{'结果':<8}")
    print("=" * 74)

    passed = failed = 0
    for text, expect, desc in cases:
        # 先把保活帧排空，等稳态（全 0 帧）出现后再清空，
        # 避免把 20Hz 背景心跳误判成"本条指令的下发"
        t0 = time.time()
        while time.time() - t0 < 0.6:
            if slave.cmds and slave.cmds[-1] == (0, 0, 0):
                break
            time.sleep(0.02)
        slave.cmds.clear()

        cfg_before = ctrl.speed
        ok, msg = ctrl.run_text_command(text)
        time.sleep(0.25)

        got = None
        t0 = time.time()
        while time.time() - t0 < 0.8:
            if slave.cmds:
                got = slave.cmds[-1]
                break
            time.sleep(0.02)

        if expect is None:
            # 未识别指令：不应产生任何"非背景"的新帧差异 —— 判据是 run_text_command 返回 False
            verdict = "✓" if not ok else "✗"
            shown = "未下发指令" if not ok else str(got)
        elif expect == 'zero':
            verdict = "✓" if (got == (0, 0, 0)) else "✗"
            shown = str(got) if got else "(无新帧)"
        elif expect == 'spd=200':
            verdict = "✓" if ctrl.speed == 200 else "✗"
            shown = f"speed={ctrl.speed}"
        elif expect == 'spd_up':
            verdict = "✓" if ctrl.speed == cfg_before + 50 else "✗"
            shown = f"speed={ctrl.speed}"
        elif expect == 'spd_dn':
            verdict = "✓" if ctrl.speed == cfg_before - 50 else "✗"
            shown = f"speed={ctrl.speed}"
        else:
            idx = {'x': 0, 'y': 1, 'z': 2}[expect[0]]
            good = got and ((got[idx] > 0) if expect[1] == '>' else (got[idx] < 0))
            verdict = "✓" if good else "✗"
            shown = str(got) if got else "(无新帧)"

        if verdict == "✓":
            passed += 1
        else:
            failed += 1
        print(f"{text:<14}{desc:<10}{shown:<22}{verdict:<8}")

    ctrl.disconnect()
    slave.stop_evt.set()

    print("=" * 74)
    print(f"  端到端闭环：通过 {passed} 项，失败 {failed} 项")
    print("=" * 74)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
