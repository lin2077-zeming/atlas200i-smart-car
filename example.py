# -*- coding: utf-8 -*-
"""
小车控制SDK - 使用示例

环境要求:
    Linux (银河麒麟 / Ubuntu) + Python 3.8+
    pip install pyserial

硬件连接:
    RK3576 USB口 -> CH340/CH343 USB转TTL模块 -> STM32F407 USART3 (PD8/PD9)
    12V电源 -> STM32F407 VIN端子

运行:
    python example.py
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from car_serial import CarSerial
from motion_control import MotionController


def demo_basic():
    """基础运动演示"""
    print("=" * 50)
    print(" 小车基础运动演示")
    print("=" * 50)

    car = CarSerial("/dev/ttyACM0")
    car.open()
    mc = MotionController(car)

    try:
        # 获取初始状态
        status = mc.get_status()
        if status:
            print(f"状态: 电压={status['voltage']:.2f}V, Vx={status['vx']}mm/s")

        # 前进 2 秒
        print("\n[1] 前进 300mm/s x 2秒")
        mc.forward(speed=300, duration=2.0)
        time.sleep(0.5)

        # 后退 1.5 秒
        print("[2] 后退 200mm/s x 1.5秒")
        mc.backward(speed=200, duration=1.5)
        time.sleep(0.5)

        # 左转 1 秒
        print("[3] 左转 200mm/s x 1秒")
        mc.turn_left(speed=200, duration=1.0)
        time.sleep(0.5)

        # 右转 1 秒
        print("[4] 右转 200mm/s x 1秒")
        mc.turn_right(speed=200, duration=1.0)
        time.sleep(0.5)

        # 停止
        print("[5] 停止")
        mc.stop()

        status = mc.get_status()
        if status:
            print(f"最终状态: 电压={status['voltage']:.2f}V, Vx={status['vx']}mm/s")

        print("\n演示完成!")

    except KeyboardInterrupt:
        print("\n中断")
    finally:
        car.close()


def demo_keyboard():
    """键盘控制演示 (wasd + q退出)"""
    print("=" * 50)
    print(" 键盘控制小车 (wasd 移动, q 退出)")
    print(" w=前进 s=后退 a=左转 d=右转 空格=停止")
    print("=" * 50)

    car = CarSerial("/dev/ttyACM0")
    car.open()
    mc = MotionController(car)

    import termios, tty, select

    old_settings = termios.tcgetattr(sys.stdin.fileno())
    tty.setcbreak(sys.stdin.fileno())

    speed = 300  # 默认速度 mm/s

    try:
        print("  就绪! 使用 wasd 控制")
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if r:
                ch = sys.stdin.read(1).lower()
                if ch == "w":
                    mc.forward(speed=speed, duration=None)
                    print(f"  前进 {speed}mm/s")
                elif ch == "s":
                    mc.backward(speed=speed, duration=None)
                    print(f"  后退 {speed}mm/s")
                elif ch == "a":
                    mc.turn_left(speed=speed, duration=None)
                    print(f"  左转 {speed}mm/s")
                elif ch == "d":
                    mc.turn_right(speed=speed, duration=None)
                    print(f"  右转 {speed}mm/s")
                elif ch == " ":
                    mc.stop()
                    print("  停止")
                elif ch == "q":
                    break
                elif ch == "1":
                    speed = 100
                    print(f"  速度: {speed}")
                elif ch == "2":
                    speed = 200
                    print(f"  速度: {speed}")
                elif ch == "3":
                    speed = 300
                    print(f"  速度: {speed}")
                elif ch == "4":
                    speed = 400
                    print(f"  速度: {speed}")

            # 实时显示状态
            fb = car.get_feedback()
            if fb:
                print(f"\r  Vx={fb['vx']:4d}mm/s 电压={fb['voltage']:.2f}V",
                      end="", flush=True)

    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        car.close()
        print("\n已退出")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "keyboard":
        demo_keyboard()
    else:
        demo_basic()
