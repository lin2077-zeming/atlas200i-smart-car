#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""展示屏「开机就有画面（免登录）」链路 · 离线回归

不需要显示器、不需要板子、不需要 root —— 只检查**链路拼接**对不对：

  ① sddm 自动登录配置 → ② 登录后自启 → ③ display_supervisor.sh 看护
  → ④ display.py 全屏（Esc 退出）

断言的核心是三件容易写错的事：
  · 自动登录必须 `Relogin=true`（否则注销/重登后回到登录框，又得输密码）
  · 自启必须指向 **看护进程**，不能直连 run_display.sh（否则崩了不回来）
  · 退出码契约：0 = 用户按 Esc（**不要再拉起**）；3 = 没有图形环境（不要空转）

平台说明
--------
看护进程的动态测试（[5]~[8]）在 **Linux** 上跑（Windows 的 bash 走 WSL，不转发
环境变量，在 Windows 侧跑只会读到默认值）；静态检查两边都跑。

跑法：
    python3 test_display_boot.py
"""
from __future__ import print_function

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(HERE, "setup_display_autostart.sh")
SUP = os.path.join(HERE, "display_supervisor.sh")
RUN = os.path.join(HERE, "run_display.sh")
DISPLAY_PY = os.path.join(HERE, "display.py")

IS_LINUX = sys.platform.startswith("linux")
_ok = _bad = 0
_fails = []
BASH = shutil.which("bash")


def check(name, cond, detail=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print("  [PASS] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        _bad += 1
        _fails.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


def sh(args, env=None, timeout=20, cwd=None):
    """跑一段 bash，返回 (rc, out)。stdout/stderr 合并。"""
    e = dict(os.environ)
    e.pop("DISPLAY", None)
    e.pop("WAYLAND_DISPLAY", None)
    if env:
        e.update(env)
    try:
        p = subprocess.run([BASH] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, cwd=cwd or HERE, env=e)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as ex:
        return 124, (ex.stdout or b"").decode("utf-8", "replace")


def sup_env(tmp, **kw):
    """看护进程的干净环境：HOME 指到临时目录，避免和板上真在跑的那个抢锁。"""
    e = {
        "HOME": _posix(tmp),
        "CAR_DISPLAY_LOG": _posix(os.path.join(tmp, "sup.log")),
        "CAR_DISPLAY_WAIT_WEB": "0",
        "CAR_DISPLAY_DELAY": "0",
        "CAR_DISPLAY_BACKOFF": "1",
        "CAR_DISPLAY_MAXFAILS": "2",
        "CAR_DISPLAY_LONG_SLEEP": "1",
    }
    e.update(kw)
    return e


_WSL = None


def _bash_mode():
    """Windows 上的 bash 可能是 Git Bash（/c/…）也可能是 WSL（/mnt/c/…）。"""
    global _WSL
    if _WSL is None:
        try:
            out = subprocess.run([BASH, "-c", "pwd"], cwd=HERE, timeout=15,
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL).stdout
            _WSL = out.decode("utf-8", "replace").strip().startswith("/mnt/")
        except Exception:
            _WSL = False
    return _WSL


def _posix(p):
    """Windows 路径 → bash 能认的形式（Linux 原样返回）。"""
    if os.name != "nt":
        return p
    p = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if not m:
        return p
    drive, rest = m.group(1).lower(), m.group(2)
    return ("/mnt/%s/%s" if _bash_mode() else "/%s/%s") % (drive, rest)


P_SETUP = P_SUP = P_RUN = None


def static_tests():
    # ── 1. 文件与语法 ─────────────────────────────────────────
    print("\n[1] 文件与语法")
    for p in (SETUP, SUP, RUN, DISPLAY_PY):
        check("存在 %s" % os.path.basename(p), os.path.isfile(p))
    for p, pp in ((SETUP, P_SETUP), (SUP, P_SUP), (RUN, P_RUN)):
        rc, out = sh(["-n", pp])
        check("语法 OK：%s" % os.path.basename(p), rc == 0, out.strip()[:120])

    # ── 2. --help 把四种装法都摆出来 ──────────────────────────
    print("\n[2] 用法（四种装法 + 回滚）")
    rc, out = sh([P_SETUP, "--help"])
    check("--help 退出码 0", rc == 0, str(rc))
    for flag in ("--boot", "--boot-kiosk", "--desktop", "--xinit",
                 "--no-autologin", "--status", "--uninstall"):
        check("帮助里有 %s" % flag, flag in out)

    # ── 3. 生成的 sddm 自动登录配置 ───────────────────────────
    print("\n[3] sddm 自动登录配置（--print-config）")
    rc, cfg = sh([P_SETUP, "--print-config"])
    check("--print-config 退出码 0", rc == 0, str(rc))
    check("写到 /etc/sddm.conf.d/50-car-autologin.conf",
          "/etc/sddm.conf.d/50-car-autologin.conf" in cfg)
    check("有 [Autologin] 段", "[Autologin]" in cfg)
    check("User= 非空", re.search(r"^User=\S+", cfg, re.M) is not None)
    check("默认会话 = lxqt（有桌面）", re.search(r"^Session=lxqt$", cfg, re.M) is not None)
    check("Relogin=true（注销/重登后仍然自动登录）",
          re.search(r"^Relogin=true$", cfg, re.M) is not None)
    check("注释里给了回滚命令", "--no-autologin" in cfg)

    # ── 4. 自启/图标/kiosk 都指向看护进程 ─────────────────────
    print("\n[4] 自启入口")
    check("自启 .desktop 在 ~/.config/autostart", ".config/autostart/car-display.desktop" in cfg)
    check("自启 Exec 指看护进程",
          re.search(r"^Exec=/bin/bash .*display_supervisor\.sh", cfg, re.M) is not None)
    check("自启带 --delay=3（等桌面/WM 就绪，否则全屏抢不到焦点）", "--delay=3" in cfg)
    check("自启有 X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-enabled=true" in cfg)
    check("桌面图标也指向看护进程", cfg.count("display_supervisor.sh") >= 3,
          "出现 %d 次" % cfg.count("display_supervisor.sh"))
    check("kiosk 会话 DesktopNames=CarDisplay", "DesktopNames=CarDisplay" in cfg)
    check("kiosk 会话落 /usr/share/xsessions/car-display.desktop",
          "/usr/share/xsessions/car-display.desktop" in cfg)
    desktop = re.search(r"^Exec=/bin/bash (\S+display_supervisor\.sh)", cfg, re.M)
    check("自启不直连 run_display.sh",
          desktop is not None and "run_display.sh" not in desktop.group(1),
          desktop.group(1) if desktop else "没找到 Exec")
    check("防息屏：在 ~/.config/autostart/ 挡掉省电/屏保守护进程",
          "防息屏" in cfg and "Hidden=true" in cfg)
    check("挡掉的名单里有 xscreensaver / lxqt-powermanagement / xfce4-power-manager",
          all(n in cfg for n in ("xscreensaver", "lxqt-powermanagement",
                                 "xfce4-power-manager")), cfg.count("Hidden=true"))

    # ── 9. display.py：Esc / F12 退出，其它键不动 ─────────────
    print("\n[9] display.py 键盘契约")
    src = open(DISPLAY_PY, encoding="utf-8").read()
    check("按 Esc 退出", "Qt.Key_Escape" in src)
    check("按 F12 也退出（面板上没有 Esc 键时用）", "Qt.Key_F12" in src)
    check("退出码 0 交给看护进程判断", "_user_quit" in src)
    try:
        probe = (
            "import os, sys\n"
            "os.environ['QT_QPA_PLATFORM'] = 'offscreen'\n"
            "sys.path.insert(0, %r)\n"
            "from PySide6.QtWidgets import QApplication\n"
            "from PySide6.QtGui import QKeyEvent\n"
            "from PySide6.QtCore import Qt, QEvent\n"
            "app = QApplication([])\n"
            "import display as D\n"
            "p = D.Panel(screen_size=(1024, 600))\n"
            "def key(k):\n"
            "    return QKeyEvent(QEvent.KeyPress, k, Qt.NoModifier)\n"
            "p.keyPressEvent(key(Qt.Key_A))\n"
            "a = p._user_quit\n"
            "p.keyPressEvent(key(Qt.Key_Escape))\n"
            "print('probe', a, p._user_quit)\n"
        ) % HERE
        env = dict(os.environ)
        env["QT_QPA_PLATFORM"] = "offscreen"
        p = subprocess.run([sys.executable, "-c", probe], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=90, env=env)
        out = p.stdout.decode("utf-8", "replace")
        check("普通按键不退出", "probe False True" in out, out.strip()[-120:])
    except Exception as ex:                                   # 没有 PySide6
        print("  [SKIP] PySide6 探测失败：%r" % (ex,))

    # ── 11. 回滚路径存在（静态检查，避免真的把配置删掉）────────
    print("\n[11] 回滚")
    setup_src = open(SETUP, encoding="utf-8").read()
    check("支持 --no-autologin", "--no-autologin" in setup_src)
    check("no-autologin 会删掉自动登录配置", 'rm -f "$AUTOLOGIN_CONF"' in setup_src)
    check("--status 能看出四种装法的状态", "show_status" in setup_src)
    check("安装时会一起装防息屏（install_noblank）", "install_noblank" in setup_src)
    rc, out = sh([P_SETUP, "--status"], timeout=20)
    check("--status 退出码 0", rc == 0, str(rc))
    for word in ("自动登录", "桌面自启", "kiosk会话", "防息屏"):
        check("--status 输出含「%s」" % word, word in out)


def dynamic_tests():
    """看护进程的实跑测试（只在 Linux 上做，见文件头）。"""
    # ── 5. 退出码 0 = 用户按 Esc → 不再拉起 ───────────────────
    print("\n[5] 看护进程 · 用户主动退出")
    with tempfile.TemporaryDirectory(dir=HERE) as tmp:
        t0 = time.time()
        rc, out = sh([P_SUP], env=sup_env(tmp, CAR_DISPLAY_CMD="exit 0", DISPLAY=":99"),
                     timeout=15)
        dur = time.time() - t0
        log = open(os.path.join(tmp, "sup.log")).read()
        check("退出码 0", rc == 0, str(rc))
        check("立刻结束（<5 s，没有空转）", dur < 5.0, "%.1fs" % dur)
        check("日志记下「用户主动退出」", "用户主动退出" in log,
              log.strip()[-80:].replace("\n", " "))

    # ── 6. 崩溃 → 自己回来 ────────────────────────────────────
    print("\n[6] 看护进程 · 崩溃自愈")
    with tempfile.TemporaryDirectory(dir=HERE) as tmp:
        cnt = os.path.join(tmp, "count")
        env = dict((k, v) for k, v in os.environ.items()
                   if k not in ("DISPLAY", "WAYLAND_DISPLAY"))
        env.update(sup_env(tmp, CAR_DISPLAY_CMD="echo x >> %s; exit 7" % _posix(cnt),
                           DISPLAY=":99"))
        p = subprocess.Popen([BASH, P_SUP], cwd=HERE, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            p.wait(timeout=6)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        n = len(open(cnt).read().split()) if os.path.exists(cnt) else 0
        log = open(os.path.join(tmp, "sup.log")).read()
        check("崩溃后被反复拉起（>=3 次）", n >= 3, "%d 次" % n)
        check("日志有「退出码 7」", "退出码 7" in log)
        check("连续失败到上限后走慢速重试", "连续失败" in log,
              log.strip()[-90:].replace("\n", " "))

    # ── 7. 没有图形环境 → 退出 3，不空转 ──────────────────────
    print("\n[7] 看护进程 · 没有图形环境")
    with tempfile.TemporaryDirectory(dir=HERE) as tmp:
        t0 = time.time()
        rc, out = sh([P_SUP], env=sup_env(tmp, CAR_DISPLAY_CMD="exit 0"), timeout=10)
        dur = time.time() - t0
        check("退出码 3（不是 0，别当成用户退出）", rc == 3, str(rc))
        check("秒退（<3 s）", dur < 3.0, "%.1fs" % dur)
        check("提示里给出手动启动方式", "run_display.sh" in out, out.strip()[:100])

    # ── 8. 单实例（两处自启不会抢屏）──────────────────────────
    print("\n[8] 看护进程 · 单实例")
    if shutil.which("flock"):
        with tempfile.TemporaryDirectory(dir=HERE) as tmp:
            env = sup_env(tmp, CAR_DISPLAY_CMD="sleep 4; exit 0", DISPLAY=":99")
            e = dict(os.environ)
            e.pop("WAYLAND_DISPLAY", None)
            e.update(env)
            first = subprocess.Popen([BASH, P_SUP], cwd=HERE, env=e,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.2)
            t0 = time.time()
            rc2, out2 = sh([P_SUP], env=env, timeout=10)
            dur = time.time() - t0
            first.kill()
            first.wait()
            log = open(os.path.join(tmp, "sup.log")).read()
            check("第二个实例立刻退出 0", rc2 == 0 and dur < 3.0, "rc=%s %.1fs" % (rc2, dur))
            check("日志说明已有实例", "已有展示屏实例" in log,
                  log.strip()[-60:].replace("\n", " "))
    else:
        print("  [SKIP] 没有 flock，跳过单实例检查")

    # ── 10. run_display.sh：无图形环境退出码 3 ────────────────
    print("\n[10] run_display.sh 环境判定")
    rc, out = sh([P_RUN], timeout=30)
    check("Linux 无 DISPLAY → 退出码 3", rc == 3, "rc=%s" % rc)
    check("提示不冒进（说明三选一）", "板子本地终端" in out or "VNC" in out)


def main():
    global P_SETUP, P_SUP, P_RUN
    print("=" * 62)
    print(" 展示屏『开机就有画面』链路 · 离线回归")
    print("=" * 62)

    if not BASH:
        print("  [SKIP] 没有 bash，跳过")
        return 0

    P_SETUP, P_SUP, P_RUN = _posix(SETUP), _posix(SUP), _posix(RUN)

    static_tests()

    if IS_LINUX:
        dynamic_tests()
    else:
        print("\n[5]~[8][10] 看护进程动态测试：只在 Linux 板子上跑"
              "（Windows 上的 bash 走 WSL，不转发环境变量）")

    print("\n" + "=" * 62)
    if _bad:
        print(" 通过 %d · 失败 %d" % (_ok, _bad))
        print(" 失败清单：")
        for n in _fails:
            print("   x %s" % n)
    else:
        print(" 通过 %d · 失败 0" % _ok)
    print("=" * 62)
    return 1 if _bad else 0


def _exit_now(code):
    """flush 后立刻退出（同 test_display.py，避免解释器收尾时写 stdout 报错）。"""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


if __name__ == "__main__":
    _exit_now(main())