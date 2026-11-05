#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_gui_tk_smoke.py —— Tkinter 备用界面的真机冒烟测试

为什么要有它（2026-09-27 踩坑后加）：
    `car_gui_tk.py` 里 `port` 变量作用域写错（NameError），**整个 Tk 备用界面
    根本起不来**，却长期没人发现 —— 因为离线回归里只有 PySide6 的测试，
    谁也没真跑过 Tk。`py_compile` 查不出这类运行期错误。

需要真实的 X 显示环境（DISPLAY），在 SSH 里要这样跑：
    su - HwHiAiUser -c "cd ~/car_gui && DISPLAY=:0 XAUTHORITY=/home/HwHiAiUser/.Xauthority \\
        bash run_py.sh test_gui_tk_smoke.py"
没有 DISPLAY 时本测试会**跳过**（返回码 2），不算失败。

只做结构检查，**不连串口、不下发任何运动指令**。
"""
import os
import sys
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = 0
FAIL = 0


def check(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s%s" % (name, ("  —— " + extra) if extra else ""))
    else:
        FAIL += 1
        print("  ✗ %s%s" % (name, ("  —— " + extra) if extra else ""))


def main():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("跳过：没有图形显示环境（DISPLAY 为空）。Tk 界面必须有 X。")
        return 2

    print("═══ Tkinter 界面冒烟测试 ═══")
    print("DISPLAY=%s" % os.environ.get("DISPLAY"))
    print()

    try:
        import car_gui_tk as G
    except Exception as e:
        print("  ✗ 导入 car_gui_tk 失败：%r" % (e,))
        return 1

    print("① 创建窗口")
    root = tk.Tk()
    try:
        # auto_tts=False：冒烟测试不加载 TTS 模型（省 5~7 秒 + 600MB 内存）
        app = G.App(root, auto_tts=False)
    except Exception as e:
        print("  ✗ 构造 App 失败（多半是运行期 bug）：%r" % (e,))
        import traceback
        traceback.print_exc()
        try:
            root.destroy()
        except Exception:
            pass
        return 1
    root.update()
    check(True, "App 构造成功（没抛 NameError 之类的运行期错误）")
    print()

    print("② 关键控件")
    for name, label in (
        ("ent_cmd", "指令输入框"),
        ("cmb_cmd", "常用指令下拉"),
        ("lab_conn", "连接状态标签"),
        ("txt_log", "运行日志区"),
        ("bar_estop", "急停警示条"),
        ("_show_estop_bar", "急停条显示/隐藏方法"),
        ("var_demo", "离线演示模式开关变量"),
        ("on_toggle_demo", "离线演示模式回调"),
        ("_refresh_title", "标题刷新方法"),
    ):
        check(hasattr(app, name), "存在 %s（%s）" % (name, label))
    print()

    print("②b 离线演示模式（未连小车也要能用）")
    try:
        check(not app.var_demo.get(), "初始关闭")
        app.var_demo.set(True)
        app.on_toggle_demo()
        root.update()
        check(app.ctrl.status().get("demo_mode") is True, "开启后 core 的 demo_mode=True")
        check("离线演示中" in root.title(), "标题提示离线演示", "标题=%r" % root.title())
        ok_demo, msg_demo = app.ctrl.run_text_command("前进一米")
        root.update()
        check(ok_demo, "演示模式下指令被接受", msg_demo)
        app.var_demo.set(False)
        app.on_toggle_demo()
        root.update()
        check(app.ctrl.status().get("demo_mode") is False, "关闭后 demo_mode=False")
        check("离线演示中" not in root.title(), "标题恢复", "标题=%r" % root.title())
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(False, "离线演示模式流程", "%r" % (e,))
    print()

    print("③ 常用指令下拉")
    vals = tuple(app.cmb_cmd.cget("values")) if hasattr(app, "cmb_cmd") else ()
    from car_gui_core import COMMON_COMMANDS
    miss = [c for c in COMMON_COMMANDS if c not in vals]
    check(len(vals) >= len(COMMON_COMMANDS) + 1,
          "下拉里有 %d 项（占位 + %d 条常用指令）" % (len(vals), len(COMMON_COMMANDS)),
          "缺失：%s" % miss if miss else "")
    # 选一条，看是否填进输入框（不自动执行）
    if COMMON_COMMANDS:
        pick = COMMON_COMMANDS[0]
        app.cmb_cmd.set(pick)
        app.on_pick_cmd()
        root.update()
        got = app.ent_cmd.get()
        check(got == pick, "选中「%s」→ 填入输入框" % pick, "实际=%r" % got)
        check(app.cmb_cmd.get().endswith("▾"), "选完自动复位到占位项")
    print()

    print("④ 急停锁定 / 解除")
    try:
        app.ctrl.trigger_emergency(reason="冒烟测试", announce=False)
        check(app.ctrl.estop_locked, "触发急停 → 进入锁定态")
        app._show_estop_bar(True)
        root.update()
        check(bool(app.bar_estop.winfo_ismapped()), "警示条显示出来",
              "winfo_ismapped=%s" % app.bar_estop.winfo_ismapped())
        app.ctrl.release_estop()
        root.update()
        check(not app.ctrl.estop_locked, "解除后锁定态释放")
        app._show_estop_bar(False)
        root.update()
        check(not bool(app.bar_estop.winfo_ismapped()), "警示条隐藏回去")
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(False, "急停锁定/解除流程", "%r" % (e,))
    print()

    try:
        app.on_close()
    except Exception:
        try:
            root.destroy()
        except Exception:
            pass

    print("────────────────────────────────")
    print("通过 %d · 失败 %d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
