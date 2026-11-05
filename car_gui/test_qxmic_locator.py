#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_qxmic_locator.py —— HK-QXMIC 声源定位麦克风串口驱动「纯离线」单元测试

不需要接硬件、不需要 pyserial、不打开任何串口，验证：
  · 协议帧：正常帧 / 分片到达 / 粘包 / 噪声混流 / 错校验 / 非法 coord /
    帧头错位（扫描式重同步）
  · 12 方位角度映射、8 方向映射（含边界值）、中文描述、安装偏移 offset
  · FrameParser 缓冲区行为、QxMicLocator 的 API 契约与懒加载
  · 无硬件时的友好报错（打不开串口不抛栈）
  · CLI：--help / --list-ports / --simulate / --json / --raw / --offset
    （用子进程真实跑一遍，等于把验收命令自动化）

用法（在项目根目录或 car_gui/ 下都行）：
    python3 car_gui/test_qxmic_locator.py
退出码：0 = 全部通过，1 = 有失败。

对应文档：《../6.说明文档/4.语音模块/声源定位麦克风.md》
"""

import itertools
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qxmic_locator as Q  # noqa: E402

SCRIPT = os.path.join(HERE, "qxmic_locator.py")

PASS = 0
FAIL = 0
FAILED_NAMES = []


# ── 测试小工具（与 car_gui 其它 self-test 风格一致：不引第三方测试框架）──
def check(name, cond, detail=""):
    """断言一条；失败不中断，最后统一汇总（一次跑完看到全部问题）。"""
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s%s" % (name, ("  " + detail) if detail else ""))
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))


def check_eq(name, got, want):
    """断言相等，失败时打印期望/实际值。"""
    check(name, got == want, "期望 %r，实际 %r" % (want, got))


def check_raises(name, exc_type, fn, *args, **kwargs):
    """断言调用抛出指定异常。"""
    try:
        fn(*args, **kwargs)
    except exc_type:
        check(name, True)
    except Exception as exc:                                  # noqa: BLE001
        check(name, False, "抛了 %r 而不是 %r" % (exc, exc_type))
    else:
        check(name, False, "没有抛异常（期望 %r）" % (exc_type,))


def frame(sector):
    """按协议拼一帧（测试里到处用）。"""
    return Q.simulated_frame(sector)


def run_cli(*argv, **kwargs):
    """用子进程跑 CLI（真实入口，顺带验证「没装 pyserial 也能跑」）。"""
    timeout = kwargs.pop("timeout", 30)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, SCRIPT] + list(argv),
        cwd=HERE, env=env, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def out_of(proc):
    """把子进程 stdout+stderr 解成文本（Windows 控制台编码可能不是 UTF-8）。"""
    def dec(b):
        return b.decode("utf-8", "replace")
    return dec(proc.stdout), dec(proc.stderr)


# ══════════════════════════════════════════════════════════════════════
def test_constants_and_angles():
    print("\n── 1. 常量与角度映射 ──")
    check_eq("BAUD = 1,000,000", Q.BAUD, 1000000)
    check("SECTOR_ANGLES 是 12 个 int 的 tuple",
          isinstance(Q.SECTOR_ANGLES, tuple) and len(Q.SECTOR_ANGLES) == 12
          and all(isinstance(a, int) for a in Q.SECTOR_ANGLES))
    check_eq("12 个中心角 (195+30k)%360", Q.SECTOR_ANGLES,
             (195, 225, 255, 285, 315, 345, 15, 45, 75, 105, 135, 165))
    check_eq("sector 0 → 195°", Q.sector_to_angle(0), 195)
    check_eq("sector 5 → 345°", Q.sector_to_angle(5), 345)
    check_eq("sector 8 → 75°", Q.sector_to_angle(8), 75)
    check_eq("sector 11 → 165°", Q.sector_to_angle(11), 165)
    check_raises("sector=12 抛 ValueError", ValueError, Q.sector_to_angle, 12)
    check_raises("sector=-1 抛 ValueError", ValueError, Q.sector_to_angle, -1)
    check_raises("sector='5' 抛 TypeError", TypeError, Q.sector_to_angle, "5")

    check_eq("厂商口径：sector 5 是「前方偏右 15°」",
             Q.describe_angle(Q.sector_to_angle(5)), "前方偏右 15°")


def test_parser_normal():
    print("\n── 2. FrameParser 正常帧 / 分片 / 粘包 ──")
    p = Q.FrameParser()
    frames = p.feed(b"\x55\xaa\x05\xfa")           # 厂商文档里的示例帧
    check_eq("正常帧解析出 1 帧", len(frames), 1)
    f = frames[0]
    check_eq("帧字段齐全", set(f.keys()), {"sector", "angle", "raw", "ts"})
    check_eq("sector = 5", f["sector"], 5)
    check_eq("angle = 345", f["angle"], 345)
    check_eq("raw 是原始 4 字节", f["raw"], b"\x55\xaa\x05\xfa")
    check("ts 是合理的 time.time()",
          isinstance(f["ts"], float) and abs(f["ts"] - time.time()) < 30)
    check_eq("解析完缓冲区清空", p.buffered, 0)

    # 分片：逐字节喂
    p = Q.FrameParser()
    raw = frame(3)
    got = []
    for i in range(len(raw)):
        got.extend(p.feed(raw[i:i + 1]))
        if i < len(raw) - 1:
            check("分片第 %d/%d 字节时还不成帧" % (i + 1, len(raw)),
                  got == [], "实际 %r" % (got,))
    check_eq("逐字节喂完只出 1 帧", len(got), 1)
    check_eq("分片帧 sector 正确", got[0]["sector"] if got else None, 3)

    # 分片：从中间断开（模拟 read() 边界）
    p = Q.FrameParser()
    a = p.feed(raw[:2])
    b = p.feed(raw[2:])
    check_eq("2+2 分片：前半不出帧", len(a), 0)
    check_eq("2+2 分片：后半出 1 帧", len(b), 1)

    # 粘包：一次喂 3 帧
    p = Q.FrameParser()
    frames = p.feed(frame(0) + frame(7) + frame(11))
    check_eq("粘包一次出 3 帧", len(frames), 3)
    check_eq("粘包顺序正确", [f["sector"] for f in frames], [0, 7, 11])
    check_eq("粘包角度正确", [f["angle"] for f in frames], [195, 45, 165])


def test_parser_noise():
    print("\n── 3. FrameParser 混流噪声 / 错校验 / 非法 coord ──")
    # 真机实测会混进 "iGo-CMD timeout" 之类的 ASCII 调试串
    p = Q.FrameParser()
    frames = p.feed(b"iGo-CMD timeout\r\n" + frame(8) + b"\x00\x11\x22")
    check_eq("噪声中扫出 1 帧", len(frames), 1)
    check_eq("噪声帧 sector 正确", frames[0]["sector"] if frames else None, 8)

    # 错校验：55 AA 05 00（校验应为 FA）
    p = Q.FrameParser()
    check_eq("错校验帧被丢弃", len(p.feed(b"\x55\xaa\x05\x00")), 0)
    check("错校验后缓冲区不残留整帧", p.buffered <= 1,
          "buffered=%d" % p.buffered)
    check_eq("错校验后还能重同步解析下一帧",
             [f["sector"] for f in p.feed(frame(5))], [5])

    # 非法 coord：12 / 255，即使校验字节凑巧对也要丢
    p = Q.FrameParser()
    check_eq("coord=12 被丢弃", len(p.feed(b"\x55\xaa\x0c\xf3")), 0)
    check_eq("coord=255 被丢弃", len(p.feed(b"\x55\xaa\xff\x00")), 0)
    check_eq("非法 coord 后仍能解析", [f["sector"] for f in p.feed(frame(1))], [1])

    # 帧头错位：前面多一个 0x55 / 假帧头，应扫描到真正的帧头
    p = Q.FrameParser()
    frames = p.feed(b"\x55\x55\xaa\x05\xfa")
    check_eq("多一个 0x55 也能扫到帧", [f["sector"] for f in frames], [5])

    p = Q.FrameParser()
    frames = p.feed(b"\x55\xaa\x55\xaa\x05\xfa")   # 假帧头 55 AA 55(非法 coord)
    check_eq("假帧头后能扫到真帧", [f["sector"] for f in frames], [5])

    # 脏数据兜底：缓冲区不能无限涨
    p = Q.FrameParser()
    p.feed(b"\x00" * 5000)
    check("大量脏字节后缓冲区有上限", p.buffered <= 2, "buffered=%d" % p.buffered)

    # reset()
    p = Q.FrameParser()
    p.feed(frame(2)[:2])
    p.reset()
    check_eq("reset() 清空半帧", p.buffered, 0)
    check_eq("reset() 后能正常解析", [f["sector"] for f in p.feed(frame(2))], [2])


def test_12_sectors():
    print("\n── 4. 12 方位全覆盖 + 8 方向映射 ──")
    expected_dirs = ("后", "右后", "右", "右", "右前", "前",
                     "前", "左前", "左", "左", "左后", "后")
    p = Q.FrameParser()
    ok_sector = ok_angle = ok_dir = True
    for k in range(12):
        frames = p.feed(frame(k))
        if len(frames) != 1 or frames[0]["sector"] != k:
            ok_sector = False
            continue
        if frames[0]["angle"] != Q.SECTOR_ANGLES[k]:
            ok_angle = False
        if Q.angle_to_direction(frames[0]["angle"]) != expected_dirs[k]:
            ok_dir = False
    check("12 个扇区都能正确解析", ok_sector)
    check("12 个扇区角度都正确", ok_angle)
    check("12 个扇区方向都正确", ok_dir,
          "期望 %r" % (expected_dirs,))
    check_eq("8 个方向名全覆盖", set(expected_dirs), set(Q.DIRECTIONS))

    # 边界值：每个方向 45°，边界取 [center-22.5, center+22.5)
    cases = [
        (0, "前"), (22.4, "前"), (22.5, "左前"), (45, "左前"),
        (67.4, "左前"), (67.5, "左"), (90, "左"), (112.4, "左"),
        (112.5, "左后"), (135, "左后"), (157.4, "左后"), (157.5, "后"),
        (180, "后"), (202.4, "后"), (202.5, "右后"), (225, "右后"),
        (247.4, "右后"), (247.5, "右"), (270, "右"), (292.4, "右"),
        (292.5, "右前"), (315, "右前"), (337.4, "右前"), (337.5, "前"),
        (359.9, "前"),
    ]
    bad = [(a, Q.angle_to_direction(a), want) for a, want in cases
           if Q.angle_to_direction(a) != want]
    check("8 方向边界值全部正确", not bad, "不符：%r" % (bad,))

    # 角度规整：负数/超过 360 也要能算
    check_eq("-15° → 前（等价 345°）", Q.angle_to_direction(-15), "前")
    check_eq("360° → 前", Q.angle_to_direction(360), "前")
    check_eq("405° → 左前（等价 45°）", Q.angle_to_direction(405), "左前")

    # 中文描述：终端一行显示用
    desc_cases = [
        (345, "前方偏右 15°"), (15, "前方偏左 15°"), (0, "前方"),
        (195, "后方偏右 15°"), (180, "后方"), (225, "右后方"),
        (90, "左方"), (135, "左后方"), (300, "右前方偏右 15°"),
    ]
    bad = [(a, Q.describe_angle(a), want) for a, want in desc_cases
           if Q.describe_angle(a) != want]
    check("角度中文描述正确", not bad, "不符：%r" % (bad,))


def test_offset():
    print("\n── 5. 安装偏移 offset ──")
    check_eq("sector 5 无偏移 = 345°",
             Q.FrameParser().feed(frame(5))[0]["angle"], 345)
    check_eq("offset=+30° → 15°",
             Q.FrameParser(offset=30).feed(frame(5))[0]["angle"], 15)
    check_eq("offset=-30° → 315°",
             Q.FrameParser(offset=-30).feed(frame(5))[0]["angle"], 315)
    check_eq("offset=180° → 165°",
             Q.FrameParser(offset=180).feed(frame(5))[0]["angle"], 165)
    check_eq("offset=360° 等价 0°",
             Q.FrameParser(offset=360).feed(frame(5))[0]["angle"], 345)
    check_eq("offset 不影响 sector 原始值",
             Q.FrameParser(offset=90).feed(frame(5))[0]["sector"], 5)
    check_eq("offset 后方向跟着变（345+90=75 → 左）",
             Q.angle_to_direction(
                 Q.FrameParser(offset=90).feed(frame(5))[0]["angle"]), "左")


def test_simulator():
    print("\n── 6. 模拟数据源（--simulate 共用）──")
    check_eq("simulated_frame(5) = 厂商示例 55 AA 05 FA",
             frame(5), b"\x55\xaa\x05\xfa")
    check_eq("simulated_frame(0) 校验字节", frame(0), b"\x55\xaa\x00\xff")
    check_raises("simulated_frame(12) 抛 ValueError", ValueError,
                 Q.simulated_frame, 12)

    seq = list(itertools.islice(Q.iter_simulated_frames(period=0), 3))
    check_eq("模拟帧顺序从 sector 5 开始（345°）",
             [f["sector"] for f in seq], [5, 6, 7])
    check_eq("模拟帧角度正确", [f["angle"] for f in seq], [345, 15, 45])

    seq = list(itertools.islice(
        Q.iter_simulated_frames(period=0, noise=True), 4))
    check_eq("带噪声模拟帧仍能解析", [f["sector"] for f in seq], [5, 6, 7, 8])


def test_locator_api():
    print("\n── 7. QxMicLocator API / 懒加载 / 友好报错 ──")
    loc = Q.QxMicLocator()
    check_eq("默认波特率 1,000,000", loc.baud, Q.BAUD)
    check_eq("默认 offset = 0", loc.offset, 0)
    check_eq("默认 timeout = 0.5", loc.timeout, 0.5)
    check_eq("未打开时 get() → None", loc.get(), None)

    t0 = time.time()
    check_eq("未打开时 wait_next() → None", loc.wait_next(timeout=0.05), None)
    check("wait_next(0.05) 会按时超时", time.time() - t0 < 0.5,
          "耗时 %.2fs" % (time.time() - t0))

    # 回调注册/注销
    cb = loc.on_event(lambda f: None)
    check("on_event 返回原回调", cb is not None)
    check("off_event 能注销已注册回调", loc.off_event(cb) is True)
    check("重复注销返回 False", loc.off_event(cb) is False)
    check_raises("on_event 传非法回调抛 TypeError", TypeError,
                 loc.on_event, 123)
    check("QxMicError 是 RuntimeError 子类", issubclass(Q.QxMicError, RuntimeError))

    # 串口不存在时：必须是 QxMicError（友好报错），而不是原始 SerialException 栈
    bad = Q.QxMicLocator(port="/dev/ttyNOPE_QXMIC")
    check_raises("打不开不存在的串口 → QxMicError", Q.QxMicError, bad.open)
    check_raises("上下文管理器打不开也抛 QxMicError", Q.QxMicError,
                 lambda: bad.__enter__())

    # find_port / list_serial_ports：没硬件、没 pyserial 时都不能崩
    port = Q.find_port()
    check("find_port() 返回 str 或 None", port is None or isinstance(port, str),
          "实际 %r" % (port,))
    ports = Q.list_serial_ports()
    check("list_serial_ports() 返回 list", isinstance(ports, list))
    if ports:
        check("串口信息字段齐全",
              set(ports[0].keys()) == {"device", "description", "hwid",
                                       "vid", "pid", "is_ch34x", "is_car"})

    # ★ 懒加载：上面的调用不能把 pyserial 绑到模块全局上
    check("pyserial 没有被顶层 import（懒加载）",
          not hasattr(Q, "serial") and not hasattr(Q, "list_ports"))

    # CLI 参数默认值
    args = Q._build_arg_parser().parse_args([])
    check_eq("CLI --baud 默认 1000000", args.baud, 1000000)
    check_eq("CLI --offset 默认 0", args.offset, 0)
    check_eq("CLI --duration 默认 0（一直跑）", args.duration, 0.0)
    check("CLI 支持全部约定参数",
          all(hasattr(args, a) for a in
              ("list_ports", "port", "baud", "offset", "json", "raw",
               "duration", "simulate", "quiet")))


def test_cli_smoke():
    print("\n── 8. CLI 冒烟（子进程真实跑）──")
    # 8.1 --help
    p = run_cli("--help")
    out, err = out_of(p)
    check_eq("--help 退出码 0", p.returncode, 0)
    check("--help 列出约定的全部参数",
          all(a in out for a in ("--list-ports", "--port", "--baud", "--offset",
                                 "--json", "--raw", "--duration", "--simulate",
                                 "--quiet")))

    # 8.2 --list-ports（没 pyserial 的机器也必须正常退出）
    p = run_cli("--list-ports")
    out, err = out_of(p)
    check_eq("--list-ports 退出码 0", p.returncode, 0)
    check("--list-ports 不抛栈", "Traceback" not in out + err)

    # 8.3 --simulate（验收命令）
    p = run_cli("--simulate", "--duration", "0.6", "--quiet")
    out, err = out_of(p)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    check_eq("--simulate 退出码 0", p.returncode, 0)
    check("--simulate 输出方向行", len(lines) >= 2,
          "实际 %d 行" % len(lines))
    check("--simulate 每行格式符合约定",
          all(re.match(r"^sector=\d+  角度=\d+°  →  .+$", ln) for ln in lines),
          "首行：%r" % (lines[0] if lines else ""))
    check_eq("--simulate 首帧与厂商示例一致",
             lines[0] if lines else "", "sector=5  角度=345°  →  前方偏右 15°")

    # 8.4 --json：每行一个合法 JSON
    p = run_cli("--simulate", "--duration", "0.5", "--json", "--quiet")
    out, err = out_of(p)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    ok_json = True
    recs = []
    for ln in lines:
        try:
            recs.append(json.loads(ln))
        except ValueError:
            ok_json = False
    check_eq("--json 退出码 0", p.returncode, 0)
    check("--json 每行都是合法 JSON", ok_json and len(recs) >= 2)
    if recs:
        check("--json 字段齐全",
              set(recs[0].keys()) == {"sector", "angle", "direction",
                                      "text", "raw", "ts"})
        check_eq("--json 首帧 sector", recs[0]["sector"], 5)
        check_eq("--json 首帧 angle", recs[0]["angle"], 345)
        check_eq("--json raw 是十六进制串", recs[0]["raw"], "55aa05fa")

    # 8.5 --offset 生效
    p = run_cli("--simulate", "--duration", "0.4", "--offset", "90", "--quiet")
    out, _ = out_of(p)
    first = [ln for ln in out.splitlines() if ln.strip()][0]
    check_eq("--offset 90 首帧角度 = 75°", first,
             "sector=5  角度=75°  →  左方偏前 15°")

    # 8.6 --raw 带上原始字节
    p = run_cli("--simulate", "--duration", "0.3", "--raw", "--quiet")
    out, _ = out_of(p)
    check("--raw 打印原始字节", "raw=55 AA 05 FA" in out, out.splitlines()[0]
          if out.splitlines() else "")

    # 8.7 真机模式：串口不存在时必须友好报错（不崩栈、退出码非 0）
    p = run_cli("--port", "/dev/ttyNOPE_QXMIC", "--duration", "0.1")
    out, err = out_of(p)
    check("无串口时退出码非 0", p.returncode != 0, "exit=%d" % p.returncode)
    check("无串口时不抛 Traceback", "Traceback" not in out + err)
    check("无串口时有中文提示", "[错误]" in err)


def main():
    print("╔" + "═" * 66 + "╗")
    print("║" + " HK-QXMIC 声源定位麦克风驱动 · 纯离线单元测试".center(58) + "║")
    print("╚" + "═" * 66 + "╝")
    print("  不需要硬件 / 不需要 pyserial，可直接在本机或板端运行。")

    t0 = time.time()
    test_constants_and_angles()
    test_parser_normal()
    test_parser_noise()
    test_12_sectors()
    test_offset()
    test_simulator()
    test_locator_api()
    test_cli_smoke()

    print("\n" + "═" * 68)
    print("  结果：通过 %d 项，失败 %d 项（耗时 %.2fs）"
          % (PASS, FAIL, time.time() - t0))
    if FAILED_NAMES:
        print("  失败项：")
        for name in FAILED_NAMES:
            print("    · %s" % name)
    print("═" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())