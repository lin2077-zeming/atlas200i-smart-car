#!/usr/bin/env python3
"""
串口链路联调测试（无需硬件）
用 pty 伪造一个串口，模拟 STM32 上位机行为：
  1) 接收 11 字节下行帧，校验帧头/帧尾/BCC
  2) 以 20Hz 回发 24 字节状态帧
验证 car_serial.py 的收发在两个方向上完全正确。
"""
import os
import pty
import struct
import sys
import threading
import time

sys.path.insert(0, '/home/HwHiAiUser/car_gui')
import car_serial
from car_serial import CarSerial, build_command, parse_feedback

FRAME_HEAD = 0x7B
FRAME_TAIL = 0x7D


def bcc(data):
    v = 0
    for b in data:
        v ^= b
    return v & 0xFF


def make_status_frame(vx=0, vy=0, vz=0, volt_mv=12300):
    """构造 24 字节上行帧"""
    buf = bytearray(24)
    buf[0] = FRAME_HEAD
    buf[1] = 0                       # Flag_Stop: 0=使能
    struct.pack_into('>h', buf, 2, int(vx))
    struct.pack_into('>h', buf, 4, int(vy))
    struct.pack_into('>h', buf, 6, int(vz))
    # IMU: acc[0..5] 6字节(short x3), gyro[0..5] 6字节(short x3)
    for i in range(6):
        buf[8 + i] = 0
        buf[14 + i] = 0
    # Vbat: 第20-21字节
    struct.pack_into('>H', buf, 20, int(volt_mv))
    buf[22] = bcc(buf[0:22])
    buf[23] = FRAME_TAIL
    return bytes(buf)


def slave_thread(master_fd, received, stop_evt):
    """模拟下位机：解析下行帧 + 20Hz 回发状态帧"""
    buf = bytearray()
    last_tx = 0.0
    vx = vy = vz = 0

    while not stop_evt.is_set():
        # 回发状态帧 @20Hz
        now = time.time()
        if now - last_tx >= 0.05:
            last_tx = now
            os.write(master_fd, make_status_frame(vx, vy, vz, 12300))

        # 非阻塞读
        import select
        r, _, _ = select.select([master_fd], [], [], 0.01)
        if r:
            chunk = os.read(master_fd, 512)
            buf.extend(chunk)

        # 从缓冲里抽 11 字节帧
        while len(buf) >= 11:
            if buf[0] != FRAME_HEAD:
                buf.pop(0)
                continue
            if buf[10] != FRAME_TAIL:
                buf.pop(0)
                continue
            frame = bytes(buf[:11])
            del buf[:11]
            # 协议：下行帧 Check_Sum(9,0) —— 对前 9 字节（含帧头 0x7B）逐字节异或
            calc = bcc(frame[0:9])
            x = struct.unpack('>h', frame[3:5])[0]
            y = struct.unpack('>h', frame[5:7])[0]
            z = struct.unpack('>h', frame[7:9])[0]
            received.append({
                'raw': frame.hex(' '),
                'x': x, 'y': y, 'z': z,
                'bcc_ok': calc == frame[9],
                'tail_ok': frame[10] == FRAME_TAIL,
            })
            vx, vy, vz = x, y, z


def main():
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    print(f"[setup] 伪串口设备: {slave_name}")

    received = []
    stop_evt = threading.Event()
    t = threading.Thread(target=slave_thread, args=(master_fd, received, stop_evt), daemon=True)
    t.start()
    time.sleep(0.3)

    print("\n=== 1. 打开串口 ===")
    car = CarSerial(port=slave_name, baud=115200)
    car.open()
    print("    串口已打开:", car._ser is not None and car._ser.is_open)
    print("    发送线程存活:", car._thread.is_alive() if car._thread else False)
    time.sleep(0.4)

    print("\n=== 2. 测试 build_command 帧结构 ===")
    frame = build_command(300, 0, 0)
    print(f"    build_command(300,0,0) = {frame.hex(' ')}")
    print(f"    长度={len(frame)} (期望 11)  帧头={frame[0]:#x} 帧尾={frame[-1]:#x}")
    assert len(frame) == 11, "帧长错误"
    assert frame[0] == 0x7B and frame[-1] == 0x7D, "帧头帧尾错误"

    print("\n=== 3. 下发三轴指令，验证下位机解析 ===")
    received.clear()
    cases = [(300, 0, 0, "前进"), (0, 0, -200, "左转"), (0, 250, 0, "左移"), (-150, 0, 0, "后退")]
    for x, y, z, name in cases:
        car.set_speed(x, y, z)
        time.sleep(0.25)
    time.sleep(0.3)

    print(f"    下位机共收到 {len(received)} 帧")
    for r in received[:8]:
        print(f"      x={r['x']:>6}  y={r['y']:>6}  z={r['z']:>6}  "
              f"BCC={'OK' if r['bcc_ok'] else 'FAIL'}  尾={'OK' if r['tail_ok'] else 'FAIL'}")

    bad = [r for r in received if not r['bcc_ok'] or not r['tail_ok']]
    print(f"    BCC/帧尾错误帧数: {len(bad)}")

    print("\n=== 4. 验证 20Hz 保活速率 ===")
    received.clear()
    t0 = time.time()
    while time.time() - t0 < 2.0:
        time.sleep(0.05)
    dt = time.time() - t0
    rate = len(received) / dt
    print(f"    {dt:.2f}s 内下发 {len(received)} 帧 → {rate:.1f} Hz")
    print(f"    下位机超时阈值约 1 秒，需 ≥1Hz；20Hz 目标 → 余量 {rate:.0f} 倍")

    print("\n=== 5. 验证上行状态帧解析 ===")
    time.sleep(0.3)
    fb = car.get_feedback()
    if fb:
        print(f"    解析结果: {fb}")
        for k in ('vx', 'vy', 'vz', 'voltage'):
            print(f"      {k:>8} = {fb.get(k)}")
    else:
        print("    ⚠️ 未取到状态帧")

    print("\n=== 6. 验证 stop() 急停 ===")
    car.stop()
    time.sleep(0.3)
    last = received[-1] if received else None
    if last:
        print(f"    停止帧: x={last['x']} y={last['y']} z={last['z']} (应全为 0)")

    print("\n=== 7. 关闭串口 ===")
    car.close()
    print("    已关闭:", not (car._ser and car._ser.is_open))

    stop_evt.set()
    time.sleep(0.2)

    print("\n" + "=" * 60)
    print("  串口链路联调完成")
    print("=" * 60)


if __name__ == '__main__':
    main()
