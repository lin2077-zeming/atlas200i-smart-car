# -*- coding: utf-8 -*-
"""
小车动作控制层
高层次API: 前进/后退/左转/右转/停止/移动指定距离

依赖:
    pip install pyserial
    from car_serial import CarSerial
"""

import time
import threading
from car_serial import CarSerial


class MotionController:
    """
    小车运动控制器

    用法:
        car = CarSerial("/dev/ttyACM0")
        car.open()

        mc = MotionController(car)
        mc.forward(speed=300, duration=2.0)   # 前进 300mm/s, 2秒
        mc.backward(speed=200, duration=1.5)  # 后退
        mc.turn_left(speed=200, duration=1.0) # 左转
        mc.turn_right(speed=200, duration=1.0)# 右转
        mc.stop()                              # 停止

        car.close()

    速度范围: 50 ~ 500 mm/s
    注意: 小车串口有2秒超时保护, 持续运动需在后台不断发送指令
    car_serial 已自动处理这一点 (后台20Hz发送)
    """

    def __init__(self, car):
        self.car = car

    def forward(self, speed=300, duration=None):
        """前进: speed mm/s, duration=None表示持续前进直到调用stop"""
        self.car.set_speed(speed, 0, 0)
        if duration is not None:
            time.sleep(duration)
            self.car.set_speed(0, 0, 0)

    def backward(self, speed=200, duration=None):
        """后退"""
        self.car.set_speed(-speed, 0, 0)
        if duration is not None:
            time.sleep(duration)
            self.car.set_speed(0, 0, 0)

    def turn_left(self, speed=200, duration=None):
        """左转 (逆时针)"""
        self.car.set_speed(0, 0, -speed)
        if duration is not None:
            time.sleep(duration)
            self.car.set_speed(0, 0, 0)

    def turn_right(self, speed=200, duration=None):
        """右转 (顺时针)"""
        self.car.set_speed(0, 0, speed)
        if duration is not None:
            time.sleep(duration)
            self.car.set_speed(0, 0, 0)

    def strafe_left(self, speed=200, duration=None):
        """左移 (仅麦轮车型支持)"""
        self.car.set_speed(0, speed, 0)
        if duration is not None:
            time.sleep(duration)
            self.car.set_speed(0, 0, 0)

    def strafe_right(self, speed=200, duration=None):
        """右移 (仅麦轮车型支持)"""
        self.car.set_speed(0, -speed, 0)
        if duration is not None:
            time.sleep(duration)
            self.car.set_speed(0, 0, 0)

    def stop(self):
        """立即停止"""
        self.car.stop()

    def get_status(self):
        """获取小车状态: 速度 / IMU / 电压"""
        fb = self.car.get_feedback()
        if fb:
            return {
                "vx": fb["vx"],           # mm/s
                "vy": fb["vy"],
                "vz": fb["vz"],
                "voltage": fb["voltage"], # V
                "accel": (fb["accel_x"], fb["accel_y"], fb["accel_z"]),
                "gyro": (fb["gyro_x"], fb["gyro_y"], fb["gyro_z"]),
            }
        return None

    def move_distance(self, direction, distance_mm, speed=300):
        """
        移动指定距离 (mm)

        direction: "forward" / "backward" / "left" / "right"
        distance_mm: 目标距离 (毫米)
        speed: 速度 (mm/s)
        """
        direction_map = {
            "forward":  (speed, 0, 0),
            "backward": (-speed, 0, 0),
            "left":     (0, 0, -speed),
            "right":    (0, 0, speed),
        }
        if direction not in direction_map:
            raise ValueError(f"不支持的方向: {direction}")

        x, y, z = direction_map[direction]
        duration = distance_mm / speed

        self.car.set_speed(x, y, z)
        time.sleep(duration)
        self.car.set_speed(0, 0, 0)
