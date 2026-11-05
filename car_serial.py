# -*- coding: utf-8 -*-
"""
STM32F407 小车串口协议封装 - 线程安全
用于 RK3576 (银河麒麟) / Linux 上位机与小车下位机通信

通信协议:
  USART3 (PD8/PD9), 115200 8N1
  下行: 11字节指令帧 (0x7B + 速度 + XOR校验 + 0x7D)
  上行: 24字节状态帧 (0x7B + 速度/IMU/电压 + XOR校验 + 0x7D)

用法:
    from car_serial import CarSerial

    car = CarSerial("/dev/ttyACM0")
    car.open()
    car.set_speed(200, 0, 0)  # 前进 200mm/s
    time.sleep(3)
    car.stop()
    car.close()
"""

import serial
import struct
import time
import threading

FRAME_HEAD = 0x7B
FRAME_TAIL = 0x7D


def _xor_checksum(data):
    """XOR 校验, 对每个字节异或"""
    result = 0
    for b in data:
        result ^= b
    return result & 0xFF


def build_command(x_speed, y_speed, z_speed):
    """
    构建11字节下行指令帧

    帧格式 (大端序):
      byte 0:   0x7B (帧头)
      byte 1-2: 保留 (0x00)
      byte 3-4: X轴线速度 (signed int16, mm/s)
      byte 5-6: Y轴线速度 (signed int16, mm/s)
      byte 7-8: Z轴角速度 (signed int16, 单位取决于车型)
      byte 9:   XOR校验 (byte 0~8 异或)
      byte 10:  0x7D (帧尾)
    """
    payload = bytearray([
        FRAME_HEAD, 0x00, 0x00,
        (x_speed >> 8) & 0xFF, x_speed & 0xFF,
        (y_speed >> 8) & 0xFF, y_speed & 0xFF,
        (z_speed >> 8) & 0xFF, z_speed & 0xFF,
    ])
    payload.append(_xor_checksum(payload))
    payload.append(FRAME_TAIL)
    return bytes(payload)


def parse_feedback(data):
    """
    解析24字节上行状态帧

    帧格式:
      byte 0:    0x7B (帧头)
      byte 1:    Flag_Stop (0=正常, 1=急停)
      byte 2-3:  X轴速度 (signed int16, mm/s)
      byte 4-5:  Y轴速度 (signed int16, mm/s)
      byte 6-7:  Z轴速度 (signed int16, mm/s)
      byte 8-9:  Accel_X (IMU加速度 raw)
      byte 10-11: Accel_Y
      byte 12-13: Accel_Z
      byte 14-15: Gyro_X (IMU陀螺仪 raw)
      byte 16-17: Gyro_Y
      byte 18-19: Gyro_Z
      byte 20-21: 电池电压 (int16, mV)
      byte 22:    XOR校验 (byte 0~21 异或)
      byte 23:    0x7D (帧尾)

    IMU数据转换:
      加速度 raw / 1671.84 = m/s^2
      陀螺仪 raw * 0.00026644 = rad/s
    """
    if len(data) < 24:
        return None
    if data[0] != FRAME_HEAD or data[23] != FRAME_TAIL:
        return None
    if _xor_checksum(data[:22]) != data[22]:
        return None

    return {
        "flag_stop": data[1],
        "vx":       struct.unpack(">h", data[2:4])[0],
        "vy":       struct.unpack(">h", data[4:6])[0],
        "vz":       struct.unpack(">h", data[6:8])[0],
        "accel_x":  struct.unpack(">h", data[8:10])[0],
        "accel_y":  struct.unpack(">h", data[10:12])[0],
        "accel_z":  struct.unpack(">h", data[12:14])[0],
        "gyro_x":   struct.unpack(">h", data[14:16])[0],
        "gyro_y":   struct.unpack(">h", data[16:18])[0],
        "gyro_z":   struct.unpack(">h", data[18:20])[0],
        "voltage":  struct.unpack(">h", data[20:22])[0] / 1000.0,
    }


class CarSerial:
    """
    小车串口通信管理器
    后台线程每50ms发送一次指令(20Hz), 防止2秒超时停机
    同时持续读取并解析24字节回传数据
    """

    def __init__(self, port="/dev/ttyACM0", baud=115200):
        self._port = port
        self._baud = baud
        self._ser = None
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

        # 当前目标速度 (mm/s)
        self._x = 0
        self._y = 0
        self._z = 0

        # 最新回传数据
        self._feedback = None
        self._fb_lock = threading.Lock()

    # ── 后台发送线程 ──
    def _sender_loop(self):
        """持续发送当前速度指令 (20Hz) + 读取回传"""
        buf = bytearray()
        while self._running:
            try:
                with self._lock:
                    cmd = build_command(self._x, self._y, self._z)
                self._ser.write(cmd)

                # 读取回传数据
                waiting = self._ser.in_waiting
                if waiting > 0:
                    buf.extend(self._ser.read(min(waiting, 256)))

                # 解析帧
                while len(buf) >= 24:
                    if buf[0] == FRAME_HEAD and buf[23] == FRAME_TAIL:
                        fb = parse_feedback(bytes(buf[:24]))
                        if fb is not None:
                            with self._fb_lock:
                                self._feedback = fb
                        buf = buf[24:]
                    else:
                        idx = buf.find(FRAME_HEAD, 1)
                        if idx > 0:
                            buf = buf[idx:]
                        elif len(buf) > 48:
                            buf.clear()
                        else:
                            break

                time.sleep(0.05)  # 20Hz
            except Exception:
                time.sleep(0.5)

    # ── 公开接口 ──
    def open(self):
        """打开串口并启动后台通信线程"""
        self._ser = serial.Serial(self._port, self._baud, timeout=0.1)
        # 发送停止指令确保安全
        for _ in range(5):
            self._ser.write(build_command(0, 0, 0))
            time.sleep(0.03)

        self._running = True
        self._thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._thread.start()

    def close(self):
        """关闭后台线程和串口 (自动发送停止指令)"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        for _ in range(10):
            try:
                self._ser.write(build_command(0, 0, 0))
            except Exception:
                pass
            time.sleep(0.03)
        try:
            self._ser.close()
        except Exception:
            pass

    def set_speed(self, x=0, y=0, z=0):
        """
        设置目标速度 (mm/s)
        x>0 前进, x<0 后退
        y>0 左移(麦轮), y<0 右移(麦轮)
        z>0 右转, z<0 左转
        安全范围: -500 ~ 500
        """
        with self._lock:
            self._x = int(x)
            self._y = int(y)
            self._z = int(z)

    def stop(self):
        """紧急停止"""
        self.set_speed(0, 0, 0)

    def get_feedback(self):
        """获取最新回传数据, 包含速度/IMU/电压"""
        with self._fb_lock:
            return self._feedback


# ── 独立测试 ──
if __name__ == "__main__":
    print("小车串口通信测试")
    car = CarSerial()
    car.open()
    try:
        print("前进 200mm/s, 3秒...")
        car.set_speed(200, 0, 0)
        for i in range(30):
            time.sleep(0.1)
            fb = car.get_feedback()
            if fb:
                print(f"  Vx={fb['vx']:4d} mm/s, 电压={fb['voltage']:.2f}V")
        car.stop()
    finally:
        car.close()
    print("测试完成")
