#!/usr/bin/env python3
"""
GUI 键盘交互自动化测试（offscreen 模式，无需显示器）
用 pty 伪造串口，把 GUI 真正跑起来，然后用 QTest 模拟按键，
验证：
  1. 界面能实例化
  2. W/A/S/D/Q/E + 方向键映射到正确动作
  3. 点动模式：按下走、松开停
  4. 空格急停
  5. [ ] 加减速
  6. 指令栏文本解析
  7. 输入法中文事件提交路径（QInputMethodEvent → QLineEdit）
  8. 常用指令下拉填入
"""
import os
import pty
import struct
import sys
import threading
import time

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

sys.path.insert(0, '/home/HwHiAiUser/car_gui')

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest

from car_gui_core import COMMON_COMMANDS

FRAME_HEAD, FRAME_TAIL = 0x7B, 0x7D


def bcc(data):
    v = 0
    for b in data:
        v ^= b
    return v & 0xFF


def make_status_frame(vx=0, vy=0, vz=0, volt=12300):
    buf = bytearray(24)
    buf[0] = FRAME_HEAD
    buf[1] = 0
    struct.pack_into('>h', buf, 2, int(vx))
    struct.pack_into('>h', buf, 4, int(vy))
    struct.pack_into('>h', buf, 6, int(vz))
    struct.pack_into('>H', buf, 20, int(volt))
    buf[22] = bcc(buf[0:22])
    buf[23] = FRAME_TAIL
    return bytes(buf)


class FakeSlave(threading.Thread):
    """伪下位机：20Hz 回发状态帧，记录收到的指令"""
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
                    os.write(self.fd, make_status_frame(vx, vy, vz))
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
                x = struct.unpack('>h', f[3:5])[0]
                y = struct.unpack('>h', f[5:7])[0]
                z = struct.unpack('>h', f[7:9])[0]
                self.cmds.append((x, y, z))
                vx, vy, vz = x, y, z


def main():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    print(f"[setup] 伪串口: {slave_name}")

    slave = FakeSlave(master_fd)
    slave.start()

    import car_gui_pyside as G

    app = QApplication.instance() or QApplication([])
    print("[1] 创建主窗口…")
    # auto_tts=False：本测试只验键盘/点动/急停，不想顺带加载 5~7 秒的 TTS 模型
    # （那会抢 CPU、占 600MB，还可能拖慢时序判断）
    win = G.MainWindow(auto_tts=False)
    win.show()
    print("    ✓ 窗口创建成功")
    print(f"    窗口标题: {win.windowTitle()}")

    # 顺带断言新增的语音播报/急停控件确实建出来了（TTS 关闭时应显示"未启用"）
    for attr in ("led_tts", "lab_tts", "chk_speak", "chk_announce",
                 "led_kws", "lab_kws", "btn_kws", "lab_web",
                 "bar_estop", "btn_release", "lab_estop_lock",
                 "chk_demo"):
        assert hasattr(win, attr), f"缺少控件 {attr}"
    assert not win.bar_estop.isVisible(), "急停锁定条初始应隐藏"
    assert not win.chk_demo.isChecked(), "离线演示模式初始应关闭"
    print("    ✓ 语音播报/急停控件齐备，急停锁定条初始隐藏")

    # ── 离线演示模式：勾选后没连车也接受指令（但绝不下发）────────
    # 这一段必须在**连接伪串口之前**跑，才是真正的"未连接"场景。
    # ⚠️ 用 win.ctrl 而不是后面才定义的局部变量 ctrl（曾经在这里踩过 UnboundLocalError）。
    print("\n[1b] 离线演示模式（未连接小车）")
    assert not win.ctrl.connected, "此刻应尚未连接"
    win.chk_demo.setChecked(True)
    QTest.qWait(120)
    assert win.ctrl.status()["demo_mode"] is True, "勾选后 demo_mode 应为 True"
    assert "离线演示中" in win.windowTitle(), \
        f"标题应提示离线演示，实际：{win.windowTitle()}"
    n_before = len(slave.cmds)
    ok_demo, msg_demo = win.ctrl.run_text_command("前进一米")
    QTest.qWait(200)
    assert ok_demo, f"演示模式下「前进一米」应被接受，实际 {msg_demo}"
    assert len(slave.cmds) == n_before, \
        f"★ 演示模式绝不能下发串口！多出 {len(slave.cmds) - n_before} 条"
    win.chk_demo.setChecked(False)
    QTest.qWait(120)
    assert win.ctrl.status()["demo_mode"] is False, "取消勾选后应关闭"
    print("    ✓ 勾选后接受指令且不下发；标题提示正确；取消后关闭")

    # 连上串口
    print("\n[2] 连接伪串口…")
    ctrl = win.ctrl
    ok, msg = ctrl.connect(slave_name)
    time.sleep(0.4)
    print(f"    connect() -> ok={ok}, msg={msg}")
    print(f"    connected = {ctrl.connected}")
    print(f"    当前速度 = {ctrl.speed} mm/s")

    def latest():
        return slave.cmds[-1] if slave.cmds else None

    def wait_for(pred, timeout=1.5):
        """轮询直到出现满足 pred 的指令，返回该指令；超时返回最后一条"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if slave.cmds and pred(slave.cmds[-1]):
                return slave.cmds[-1]
            QTest.qWait(25)
        return slave.cmds[-1] if slave.cmds else None

    # ── 逐键测试 ───────────────────────────────────────────────
    print("\n[3] 键盘方向键测试（按下 → 检查下发 → 松开 → 检查停止）")
    keys = [
        (Qt.Key_W, 'W  前进',   ('x', '>0')),
        (Qt.Key_S, 'S  后退',   ('x', '<0')),
        # Z+ = 逆时针 = 左转（2026-09-27 实机目视确认，见 car_gui_core.ACTION_XYZ）
        (Qt.Key_A, 'A  左转',   ('z', '>0')),
        (Qt.Key_D, 'D  右转',   ('z', '<0')),
        (Qt.Key_Q, 'Q  左移',   ('y', '>0')),
        (Qt.Key_E, 'E  右移',   ('y', '<0')),
    ]
    IDX = {'x': 0, 'y': 1, 'z': 2}
    results = []
    for key, name, (axis, cond) in keys:
        slave.cmds.clear()
        idx = IDX[axis]
        # 等串口保活把"全 0"先发出来，作为基线
        wait_for(lambda c: all(v == 0 for v in c), timeout=1.0)
        slave.cmds.clear()

        QTest.keyPress(win, key)
        # 等到目标轴出现非零值
        pressed = wait_for(lambda c: (c[idx] > 0) if cond == '>0' else (c[idx] < 0))
        pv = pressed[idx] if pressed else 0

        slave.cmds.clear()
        QTest.keyRelease(win, key)
        # 等到回零
        released = wait_for(lambda c: all(v == 0 for v in c))
        rv = released[idx] if released else 999

        ok_axis = (pv > 0) if cond == '>0' else (pv < 0)
        ok_stop = all(v == 0 for v in (released or (9, 9, 9)))
        results.append((name, pv, rv, ok_axis, ok_stop))
        print(f"    {name:10s} 按下→x={pressed[0]:>5} y={pressed[1]:>5} z={pressed[2]:>5}"
              f"  松开→x={released[0]:>4} y={released[1]:>4} z={released[2]:>4}"
              f"  方向{'✓' if ok_axis else '✗'} 停住{'✓' if ok_stop else '✗'}")

    # ── 空格急停 ───────────────────────────────────────────────
    print("\n[4] 空格急停")
    slave.cmds.clear()
    QTest.keyPress(win, Qt.Key_W)
    before = wait_for(lambda c: c[0] > 0)
    QTest.keyRelease(win, Qt.Key_W)
    slave.cmds.clear()
    QTest.keyPress(win, Qt.Key_Space)
    QTest.qWait(60)
    QTest.keyRelease(win, Qt.Key_Space)
    after = wait_for(lambda c: all(v == 0 for v in c))
    print(f"    急停前 (x={before[0]}) → 急停后 (x={after[0]} y={after[1]} z={after[2]})")
    stop_ok = all(v == 0 for v in after)
    print(f"    急停{'✓ 生效' if stop_ok else '✗ 未生效'}")

    # ── 加减速 ─────────────────────────────────────────────────
    print("\n[5] [ ] 加减速")
    sp0 = ctrl.speed
    QTest.keyPress(win, Qt.Key_BracketRight)
    QTest.keyRelease(win, Qt.Key_BracketRight)
    QTest.qWait(120)
    sp1 = ctrl.speed
    QTest.keyPress(win, Qt.Key_BracketLeft)
    QTest.keyRelease(win, Qt.Key_BracketLeft)
    QTest.qWait(120)
    sp2 = ctrl.speed
    print(f"    初始 {sp0} → ]加速 {sp1} → [减速 {sp2}")
    print(f"    {'✓ 步长符合 SPEED_STEP' if sp1 - sp0 == 50 else '⚠ 步长异常'}")

    # ── 文本指令 ───────────────────────────────────────────────
    print("\n[6] 文本指令执行")
    slave.cmds.clear()
    ctrl.run_text_command('前进一米')
    QTest.qWait(400)
    got = latest()
    print(f"    '前进一米' → 下发 {got}")
    txt_ok = got and got[0] > 0
    print(f"    {'✓' if txt_ok else '✗'}")

    # ── 输入法中文提交路径（验证 GUI 能接收输入法 commit 事件）───
    print("\n[7] 输入法中文提交路径")
    win.txt_cmd.clear()
    ev = QInputMethodEvent()
    ev.setCommitString("前进一米")
    QApplication.sendEvent(win.txt_cmd, ev)
    QTest.qWait(80)
    im_text = win.txt_cmd.text().strip()
    print(f"    QInputMethodEvent 提交 '前进一米' → 输入框显示 {im_text!r}")
    im_ok = im_text == "前进一米"
    print(f"    {'✓' if im_ok else '✗'}")

    # ── 常用指令下拉 ─────────────────────────────────────────────
    print("\n[8] 常用指令下拉")
    win.txt_cmd.clear()
    win.cmb_cmd.setCurrentIndex(1)   # 第一项为占位，第二项为第一条常用指令
    QTest.qWait(80)
    drop_text = win.txt_cmd.text().strip()
    expect = COMMON_COMMANDS[0]
    print(f"    下拉选择 '{expect}' → 输入框显示 {drop_text!r}")
    drop_ok = drop_text == expect
    print(f"    {'✓' if drop_ok else '✗'}")

    # ── 设备框误输入防护 ─────────────────────────────────────────
    # 实测踩到：先点中「设备」框再打中文 → 文本累积成 /dev/ttyCAR速度200
    print("\n[9] 设备框误输入防护")
    win.cmb_port.clearEditText()
    QTest.qWait(60)
    before = win.cmb_port.currentText()
    # 模拟一次"粘贴整串"（模拟真实误输入：中文一次性进入编辑框）
    win.cmb_port.setEditText("速度200")
    QTest.qWait(120)
    after = win.cmb_port.currentText()
    guard_ok = ("速度" not in after)
    print(f"    注入 '速度200' → 设备框现在是 {after!r}（注入前 {before!r}）")
    print(f"    {'✓ 中文被拒绝，没有累积进设备名' if guard_ok else '✗ 中文漏进了设备名'}")
    # 手输合法路径必须仍然允许（不能把正常功能一起挡掉）
    win.cmb_port.setEditText("/dev/ttyUSB3")
    QTest.qWait(120)
    manual_ok = win.cmb_port.currentText().strip() == "/dev/ttyUSB3"
    print(f"    手输 '/dev/ttyUSB3' → {win.cmb_port.currentText()!r} "
          f"{'✓ 仍允许手填' if manual_ok else '✗ 手填被误挡'}")

    print("\n" + "=" * 60)
    all_ok = (all(r[3] and r[4] for r in results) and stop_ok and txt_ok
              and im_ok and drop_ok and guard_ok and manual_ok)
    print(f"  键盘交互测试：{'全部通过 ✓' if all_ok else '存在失败项 ✗'}")
    print("=" * 60)

    slave.stop_evt.set()
    ctrl.disconnect()
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
