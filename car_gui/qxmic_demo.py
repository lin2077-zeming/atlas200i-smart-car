#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qxmic_demo.py —— HK-QXMIC 声源定位麦克风「终端可视化」演示

把 qxmic_locator 读到的方位帧画成：大字方向箭头 + 12 格罗盘，
一眼看出声源在车的前/后/左/右哪一边。没有硬件也能跑（--simulate）。

用法
----
    python3 qxmic_demo.py --simulate --duration 6     # 无硬件演示（推荐先跑这个）
    python3 qxmic_demo.py --simulate                  # 一直跑，Ctrl-C 退出
    python3 qxmic_demo.py --port /dev/ttyMIC          # 真机（默认自动找 CH34x）
    python3 qxmic_demo.py --port /dev/ttyMIC --offset 90   # 安装偏移补偿

声源方向控车（可选，默认只是「干跑」打印，**不下发任何串口**）
--------------------------------------------------------------
    python3 qxmic_demo.py --drive --drive-port /dev/ttyCAR

    · 默认 dry-run：只打印「将会下发什么」，车绝对不会动。
    · 加 --drive 才真的连 CarSerial 发串口。安全限制写死在 CarDriver 里：
        线性速度 ≤150 mm/s；单次动作 ≤1.2s；0.6s 没新声源帧立即停车；
        Ctrl-C / 异常退出必经 finally 停车。
    · 角度 → 速度的映射：0°=正前，角度逆时针（向左）为正；
        x = speed*cos(θ)，z = -speed*6*sin(θ)（z>0 右转，与 car_serial 一致）。

对应文档：《../6.说明文档/4.语音模块/声源定位麦克风.md》
"""

import argparse
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import qxmic_locator as Q  # noqa: E402

#: 大字方向箭头（8 方向 × 3 行 ASCII）。方向名与 qxmic_locator.DIRECTIONS 对齐。
_ARROW_ART = {
    "前":   ("      ▲      ",
             "     ╱ ╲     ",
             "    ╱   ╲    "),
    "左前": ("  ▲          ",
             "    ╲        ",
             "      ╲      "),
    "左":   ("             ",
             "  ◀════════  ",
             "             "),
    "左后": ("      ╱      ",
             "    ╱        ",
             "  ▼          "),
    "后":   ("    ╲   ╱    ",
             "     ╲ ╱     ",
             "      ▼      "),
    "右后": ("  ╲          ",
             "    ╲        ",
             "      ▼      "),
    "右":   ("             ",
             "  ════════▶  ",
             "             "),
    "右前": ("          ▲  ",
             "        ╱    ",
             "      ╱      "),
}

# ── 声源方向控车的安全常量（写死，不开放命令行调大）──────────────────
#: 线性速度硬上限（mm/s）—— 比工程默认 DEFAULT_SPEED=300 更保守。
MAX_SPEED = 150
#: z 轴转向命令上限（与 car_gui_core.TURN_CMD=1600 同量级）。
MAX_TURN = 1600
#: 单次动作最长持续时间（秒）：到点必停车，避免「一直冲着声源跑」。
MAX_PULSE_S = 1.2
#: 超过这么久没收到新的声源帧 → 立即停车（安全看门狗）。
FRAME_STALE_S = 0.6
#: 两个动作脉冲之间的最短间隔（秒）。
PULSE_COOLDOWN_S = 0.2
#: 转向命令斜率：z = -线性速度 × TURN_GAIN × sin(偏角)。
TURN_GAIN = 6


# ── 小车联动 ────────────────────────────────────────────────────────────
def _load_car_serial_class():
    """
    延迟导入 car_serial.CarSerial（只有 --drive 才需要）。

    支持两种摆放：car_serial.py 与本文件同目录，或放在上一级（项目根目录）。
    """
    for p in (HERE, os.path.dirname(HERE)):
        if os.path.exists(os.path.join(p, "car_serial.py")):
            if p not in sys.path:
                sys.path.insert(0, p)
            break
    try:
        import car_serial
        return car_serial.CarSerial
    except Exception:
        return None


class CarDriver:
    """
    把「声源方向」翻译成小车速度指令并（可选）真的下发。

    dry_run=True（默认）时只计算/打印，不碰串口 —— 保证没小车时也绝对安全。
    安全策略：
      · 线性速度硬限 MAX_SPEED=150mm/s，转向硬限 MAX_TURN；
      · 单次动作 ≤ MAX_PULSE_S=1.2s，到期自动停车并短暂冷却；
      · FRAME_STALE_S=0.6s 内没有新声源帧 → 立即停车（声源/串口断了车不能瞎跑）。
    """

    def __init__(self, port=None, speed=120, dry_run=True):
        self.port = port
        self.speed = max(0, min(int(speed), MAX_SPEED))
        self.dry_run = dry_run
        self.car = None
        self.status = "未连接"
        self._pulse_start = None
        self._last_frame_ts = 0.0
        self._cooldown_until = 0.0

    # ── 角度 → 速度 ──
    @staticmethod
    def vector(angle, speed):
        """
        角度（0°=正前，逆时针向左为正）→ (x, z) 速度指令。

        x: 前进/后退 mm/s；z: 转向命令（z>0 右转，与 car_serial.set_speed 一致）。
        """
        rad = math.radians(float(angle) % 360.0)
        x = int(round(speed * math.cos(rad)))
        z = int(round(-speed * TURN_GAIN * math.sin(rad)))
        return (max(-MAX_SPEED, min(MAX_SPEED, x)),
                max(-MAX_TURN, min(MAX_TURN, z)))

    # ── 连接 / 关闭 ──
    def connect(self):
        """建立（或模拟建立）小车连接；失败抛 QxMicError（CLI 友好退出）。"""
        if self.dry_run:
            self.status = ("干跑（未连接小车，只打印不下发；加 --drive 才会真发串口）")
            return True
        CarSerial = _load_car_serial_class()
        if CarSerial is None:
            raise Q.QxMicError(
                "找不到 car_serial.py 或 pyserial 未安装，无法联动小车。\n"
                "  --drive 需要项目根目录的 car_serial.py + pyserial；"
                "只想看声源方向的话去掉 --drive 即可（默认干跑）。"
            )
        port = self.port or "/dev/ttyCAR"
        try:
            self.car = CarSerial(port=port, baud=115200)
            self.car.open()
        except Exception as exc:
            self.car = None
            raise Q.QxMicError(
                "连接小车失败（%s）：%s\n"
                "  排查：小车是否上电？设备名是否 /dev/ttyCAR（--drive-port 可改）？"
                % (port, exc)
            )
        try:
            self.car.stop()                     # 连上先发停车，防止残留速度
        except Exception:
            pass
        self.status = "已连接 %s（限速 ≤%dmm/s，单次 ≤%.1fs）" % (
            port, MAX_SPEED, MAX_PULSE_S)
        return True

    # ── 每帧更新 ──
    def update(self, frame):
        """按最新声源帧计算并（可选）下发速度；返回给界面显示的状态文本。"""
        now = time.time()
        self._last_frame_ts = now
        x, z = self.vector(frame["angle"], self.speed)

        if self.dry_run:
            self.status = "干跑 x=%+d z=%+d（未下发，--drive 才发串口）" % (x, z)
            return self.status

        # 单次动作到期：先停车，再进入冷却，之后才允许下一次动作
        if self._pulse_start is not None and now - self._pulse_start >= MAX_PULSE_S:
            self._stop("单次动作到期（≤%.1fs），已停车" % MAX_PULSE_S)
            self._cooldown_until = now + PULSE_COOLDOWN_S
        if now < self._cooldown_until:
            self.status = "已停车，冷却中（%.0f ms 后允许下一次动作）" % (
                (self._cooldown_until - now) * 1000)
            return self.status
        if self._pulse_start is None:
            self._pulse_start = now
        try:
            self.car.set_speed(x, 0, z)
        except Exception as exc:
            self.status = "下发失败：%s" % exc
            return self.status
        self.status = "已下发 x=%+d z=%+d（本脉冲剩余 %.1fs）" % (
            x, z, MAX_PULSE_S - (now - self._pulse_start))
        return self.status

    # ── 看门狗（没有新帧时由主循环调用）──
    def tick(self):
        """没有新声源帧时检查超时；返回 True 表示状态变了（需要重绘）。"""
        if self.dry_run or self.car is None or self._pulse_start is None:
            return False
        now = time.time()
        if now - self._pulse_start >= MAX_PULSE_S:
            self._stop("单次动作到期（≤%.1fs），已停车" % MAX_PULSE_S)
            self._cooldown_until = now + PULSE_COOLDOWN_S
            return True
        if self._last_frame_ts and now - self._last_frame_ts > FRAME_STALE_S:
            self._stop("超过 %.1fs 没有新声源帧，已停车（安全看门狗）"
                       % FRAME_STALE_S)
            return True
        return False

    # ── 内部停车 ──
    def _stop(self, reason=""):
        """立刻发停车指令并把脉冲状态复位。"""
        self._pulse_start = None
        if self.car is not None:
            try:
                self.car.stop()
            except Exception:
                pass
        if reason:
            self.status = "已停车：" + reason

    # ── 关闭 ──
    def close(self):
        """退出前必调：先停车，再关串口（异常也不外抛）。"""
        self._stop()
        car, self.car = self.car, None
        if car is not None:
            try:
                car.stop()
            except Exception:
                pass
            try:
                car.close()
            except Exception:
                pass
        self.status = "已关闭"

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


# ── 界面渲染 ────────────────────────────────────────────────────────────
def _compass_lines(active_sector, color=False):
    """
    画 12 格罗盘（环形展开：上行扇区号，下行中心角），并高亮当前扇区。

    格子顺序就是协议扇区号 0~11（0 在 195°，5 在 345°…），
    所以「前」在中间偏右处，一眼能对上角度表。
    """
    active_sector = int(active_sector)

    def row(values, fmt):
        cells = []
        for k, v in enumerate(values):
            text = fmt % v
            if k == active_sector and color:
                # ANSI 颜色序列不占可见宽度，所以格子仍然对齐
                text = "\033[1;42;30m%s\033[0m" % text
            cells.append(text)
        return "  │" + "│".join(cells) + "│"

    top = "  ┌" + "───┬" * 11 + "───┐"
    mid = "  ├" + "───┼" * 11 + "───┤"
    bot = "  └" + "───┴" * 11 + "───┘"
    pointer = " " * (4 + 4 * active_sector) + "▲ 当前扇区"
    return [
        top,
        row(range(12), "%2d "),                      # 扇区号（宽 3）
        mid,
        row(Q.SECTOR_ANGLES, "%3d"),                 # 中心角（宽 3）
        bot,
        pointer,
    ]


def render_block(frame, args, driver, count, elapsed, color):
    """把一帧渲染成多行文本块（大字箭头 + 信息 + 12 格罗盘）。"""
    angle = frame["angle"]
    direction = Q.angle_to_direction(angle)
    text = Q.describe_angle(angle)
    mode = "模拟模式（无硬件）" if args.simulate else "真机模式（%s）" % (args.port or "自动找")
    lines = [
        "═" * 62,
        " HK-QXMIC 声源定位麦克风演示    %s" % mode,
        " 第 %d 帧 · 已运行 %.1fs · 扇区 %d · 角度 %d° · 8 方向「%s」"
        % (count, elapsed, frame["sector"], angle, direction),
        "─" * 62,
    ]
    lines.extend(_ARROW_ART[direction])
    lines.append("")
    lines.append("   %s" % text)
    if driver is not None:
        lines.append("   驱动：%s" % driver.status)
    lines.append("")
    lines.append("  12 格罗盘（上：扇区号 0~11；下：中心角）")
    lines.extend(_compass_lines(frame["sector"], color))
    lines.append("")
    lines.append("   Ctrl-C 退出 · 不加 --drive 时车不会动")
    return lines


def _draw(lines, interactive):
    """输出一个文本块：交互终端原地重绘，管道/重定向时逐块追加。"""
    text = "\n".join(lines) + "\n"
    if interactive:
        sys.stdout.write("\033[H\033[J" + text)
    else:
        sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _enable_windows_ansi():
    """Windows 老控制台默认不认 ANSI 转义；尝试开启虚拟终端处理（失败就算了）。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass


# ── 数据源 ──────────────────────────────────────────────────────────────
def iter_frames(args):
    """
    统一的数据源：--simulate 用内置模拟帧，否则开真串口。

    真机模式下超时会 yield None（让上层跑停车看门狗），模拟模式不会。
    """
    if args.simulate:
        for frame in Q.iter_simulated_frames(period=0.25, offset=args.offset):
            yield frame
        return

    loc = Q.QxMicLocator(port=args.port, baud=args.baud,
                         offset=args.offset, timeout=0.5)
    loc.open()
    try:
        while True:
            frame = loc.wait_next(timeout=0.2)
            if frame is None and loc.error:
                raise Q.QxMicError("读串口出错：%s" % loc.error)
            yield frame
    finally:
        loc.close()


# ── CLI ─────────────────────────────────────────────────────────────────
def _build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="qxmic_demo.py",
        description="HK-QXMIC 声源定位麦克风终端可视化演示（大字箭头 + 12 格罗盘）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 qxmic_demo.py --simulate --duration 6\n"
               "  python3 qxmic_demo.py --port /dev/ttyMIC\n"
               "  python3 qxmic_demo.py --drive --drive-port /dev/ttyCAR\n"
               "\n对应文档：《6.说明文档/4.语音模块/声源定位麦克风.md》",
    )
    ap.add_argument("--simulate", action="store_true",
                    help="无硬件演示：用内置模拟帧（不打开串口）")
    ap.add_argument("--port", default=None,
                    help="麦克风串口（默认自动找 /dev/ttyMIC 或 CH34x）")
    ap.add_argument("--baud", type=int, default=Q.BAUD,
                    help="波特率，默认 1000000（厂商固定值）")
    ap.add_argument("--offset", type=int, default=0,
                    help="安装偏移（度），麦克风装歪时补偿，默认 0")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="运行多少秒后自动结束；0 = 一直跑到 Ctrl-C")
    ap.add_argument("--drive", action="store_true",
                    help="真的联动小车（不加则默认干跑：只打印，绝不下发串口）")
    ap.add_argument("--drive-port", default=None,
                    help="小车串口，默认 /dev/ttyCAR（配合 --drive 使用）")
    ap.add_argument("--speed", type=int, default=120,
                    help="前进速度 mm/s，默认 120（硬上限 %d）" % MAX_SPEED)
    ap.add_argument("--quiet", action="store_true",
                    help="不打印启动/结束提示，只保留画面")
    return ap


def main(argv=None):
    args = _build_arg_parser().parse_args(argv)
    if args.speed <= 0:
        print("[错误] --speed 必须是正数（单位 mm/s）", file=sys.stderr)
        return 2
    if args.speed > MAX_SPEED:
        print("[警告] --speed 超过安全上限，已压到 %d mm/s" % MAX_SPEED,
              file=sys.stderr)
        args.speed = MAX_SPEED

    interactive = sys.stdout.isatty()
    color = interactive and not os.environ.get("NO_COLOR")
    if interactive:
        _enable_windows_ansi()
    deadline = (time.time() + args.duration) if args.duration > 0 else None

    if not args.quiet:
        print("  HK-QXMIC 声源定位演示启动：%s"
              % ("模拟模式（无硬件）" if args.simulate
                 else "真机模式 %s" % (args.port or "自动找")))
        print("  %s" % ("干跑：只打印方向，不会动车（加 --drive 才联动 CarSerial）"
                        if not args.drive else
                        "⚠️ --drive 已开启：会按声源方向真的驱动小车，"
                        "限速 ≤%dmm/s、单次 ≤%.1fs，Ctrl-C 必停" % (MAX_SPEED, MAX_PULSE_S)))
        print("  Ctrl-C 退出。")
        if args.simulate and args.drive:
            print("  ⚠️ 模拟声源 + --drive：会按假方向真驱动小车，仅限台架测试！",
                  file=sys.stderr)

    driver = None
    gen = None
    count = 0
    t0 = time.time()
    try:
        driver = CarDriver(port=args.drive_port, speed=args.speed,
                           dry_run=not args.drive)
        driver.connect()

        gen = iter_frames(args)
        for frame in gen:
            now = time.time()
            if frame is None:                   # 真机模式下的等待超时
                if driver.tick():
                    lines = ["  " + driver.status]
                    _draw(lines, interactive)
                if deadline is not None and now >= deadline:
                    break
                continue

            count += 1
            driver.update(frame)
            _draw(render_block(frame, args, driver, count, now - t0, color),
                  interactive)
            if deadline is not None and time.time() >= deadline:
                break
    except Q.QxMicError as exc:
        print("[错误] %s" % exc, file=sys.stderr)
        if args.simulate and args.drive:
            print("  提示：--simulate 只替换声源数据（不接麦克风），--drive 仍会连"
                  "真实小车；请排查小车串口，或去掉 --drive 只看方向。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n  已停止（Ctrl-C）。")
    finally:
        # ★ 无论正常结束、Ctrl-C 还是异常，都必须先停车再关串口
        if driver is not None:
            driver.close()
        if gen is not None:
            try:
                gen.close()
            except Exception:
                pass

    if not args.quiet:
        print("  结束：共显示 %d 帧。" % count)
    return 0


if __name__ == "__main__":
    sys.exit(main())