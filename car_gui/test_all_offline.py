#!/usr/bin/env python3
"""
test_all_offline.py —— 无需硬件的一键回归测试

把下面几组独立的联调测试串起来跑，用于每次改动代码后的快速自检：
  0. env_doctor.py           Python 解释器体检（本板多套 Python，防止用错）
  1. selftest_offline.py     纯逻辑断言
  2. test_serial_loopback.py 串口协议双向验证（伪下位机 + pty）
  3. test_voice_estop.py     **语音急停/播报链路断言**（假车，锁死"先停车后播报"）
  3b. test_demo_offline.py   **离线演示模式断言**（假车，锁死"离线绝不下发串口"）
  3c. test_llm_integration.py **本地 LLM 接入断言**（假 LLM，锁死"急停不经 LLM"）
  4. test_gui_keyboard.py    GUI 键盘/点动/急停（offscreen 模式）
  5. test_e2e_voice.py       文本指令端到端闭环
  6. asr_preflight.py        ASR 环境预检（依赖/声卡/模型/识别/解析）
  7. kws_preflight.py        KWS 语音急停预检（依赖/模型/关键词/真人样本/麦克风）

> 第 3 项是**安全断言**：它不依赖硬件、不加载模型，跑起来不到 1 秒，
> 所以放进 `--quick` 也会跑 —— "先停车、后播报"这个顺序必须每次都验。
> 第 3b 项同理：**"离线演示模式绝不发串口"** 也是安全性质，`--quick` 也跑。

用法：
    python3 test_all_offline.py            # 全部跑
    python3 test_all_offline.py --quick    # 只跑 0+1+2（跳过 GUI 与 ASR/KWS，最快）
    python3 test_all_offline.py --no-asr   # 跳过第 5 项（ASR 预检较慢）
    python3 test_all_offline.py --no-kws   # 跳过第 6 项（KWS 预检，要开麦克风）
    python3 test_all_offline.py --no-env   # 跳过第 0 项（解释器体检）

注意：
  · GUI 测试需要 PySide6；没有 Qt 会自动跳过该项。
  · ASR 预检在**无模型时自动跳过**耗时的 L3~L5；无串口/无小车不影响。
  · KWS 预检的麦克风部分算**软失败**，无声卡时不会让整体返回非 0。
  · 第 0 项只做体检不做安装；解释器本身有硬依赖缺失时不会让整体失败，
    因为那属于「环境待装」而不是「代码坏了」。
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def run(name, script, extra_env=None, timeout=300):
    print("\n" + "═" * 74)
    print(f"  ▶ {name}")
    print("═" * 74)
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        print(f"  [跳过] 找不到 {script}")
        return None

    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    t0 = time.time()
    try:
        p = subprocess.run(
            [sys.executable, path],
            cwd=HERE, env=env, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        out = p.stdout.decode('utf-8', 'replace')
        # 过滤 Qt 的无害噪声
        lines = [l for l in out.splitlines()
                 if 'propagateSizeHints' not in l and 'cpuid_info' not in l]
        print("\n".join(lines[-40:]))
        dt = time.time() - t0
        print(f"\n  [{'通过' if p.returncode == 0 else '失败'}] {name}  ({dt:.1f}s, exit={p.returncode})")
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"  [超时] {name} 超过 {timeout}s")
        return False


def main():
    quick = '--quick' in sys.argv
    no_asr = '--no-asr' in sys.argv
    no_kws = '--no-kws' in sys.argv
    no_env = '--no-env' in sys.argv

    print("╔" + "═" * 72 + "╗")
    print("║" + "小车 GUI / ASR / KWS / TTS 离线回归测试（无需硬件）".center(66) + "║")
    print("╚" + "═" * 72 + "╝")

    results = []

    # 第 0 项：解释器体检。用不带 --write 的方式只读运行；它永远返回 0，
    # 所以不会因为「依赖没装」把整体判成失败（那是环境问题不是代码问题）。
    if no_env:
        print("\n  [跳过] --no-env，跳过解释器体检")
        results.append(("解释器体检", None))
    else:
        results.append(("解释器体检", run(
            "Python 解释器体检（本板多套 Python，防止用错）", "env_doctor.py")))

    results.append(("纯逻辑自测", run("纯逻辑自测（selftest_offline）", "selftest_offline.py")))
    results.append(("串口协议联调", run("串口协议双向联调（伪下位机）", "test_serial_loopback.py")))
    # 安全断言，跑起来不到 1 秒，所以 --quick 也跑
    results.append(("语音急停链路", run(
        "语音急停/播报顺序断言（假车，锁死「先停车后播报」）",
        "test_voice_estop.py")))
    # 同样是安全断言：离线演示模式**绝不能下发串口**（车不可能动）
    results.append(("离线演示模式", run(
        "离线演示模式断言（假车，锁死「离线绝不下发串口」）",
        "test_demo_offline.py")))
    # 也是安全断言：LLM 只做增强，急停绝不经 LLM、低置信度不下发、失败即退回旧行为
    results.append(("LLM 接入", run(
        "本地 LLM 接入断言（假 LLM，锁死「急停不经 LLM / 低置信度不下发」）",
        "test_llm_integration.py")))

    if not quick:
        try:
            import PySide6  # noqa
            has_qt = True
        except Exception:
            has_qt = False
        if has_qt:
            results.append(("GUI 键盘交互", run(
                "GUI 键盘/点动/急停（offscreen）", "test_gui_keyboard.py",
                extra_env={'QT_QPA_PLATFORM': 'offscreen'})))
        else:
            print("\n  [跳过] 未安装 PySide6，跳过 GUI 测试")
            results.append(("GUI 键盘交互", None))

        # Tkinter 备用界面冒烟（要真 X 显示；没有 DISPLAY 就跳过）
        # 为什么必须有这一项：car_gui_tk.py 曾因 `port` 变量作用域写错而
        # **整个界面起不来**，却长期没人发现 —— 回归只测 PySide6。
        # py_compile 查不出这类运行期错误，只能真跑一次。
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            results.append(("Tk 界面冒烟", run(
                "Tkinter 备用界面冒烟（控件/常用指令下拉/急停条）",
                "test_gui_tk_smoke.py", timeout=180)))
        else:
            print("\n  [跳过] 没有 DISPLAY，跳过 Tkinter 界面冒烟"
                  "（要跑：DISPLAY=:0 XAUTHORITY=/home/HwHiAiUser/.Xauthority）")
            results.append(("Tk 界面冒烟", None))

        results.append(("端到端闭环", run("文本指令端到端闭环", "test_e2e_voice.py")))

        if no_asr:
            print("\n  [跳过] --no-asr，跳过 ASR 环境预检")
            results.append(("ASR 环境预检", None))
        else:
            # ASR 预检用 no_model=False，但脚本内部会判断模型是否存在并自动降级
            results.append(("ASR 环境预检", run(
                "ASR 环境预检（依赖/声卡/模型/识别/解析）", "asr_preflight.py",
                timeout=420)))

        if no_kws:
            print("\n  [跳过] --no-kws，跳过 KWS 语音急停预检")
            results.append(("KWS 语音急停预检", None))
        else:
            results.append(("KWS 语音急停预检", run(
                "KWS 语音急停预检（依赖/模型/关键词/真人样本/麦克风）",
                "kws_preflight.py", timeout=300)))

    print("\n" + "═" * 74)
    print("  汇总")
    print("═" * 74)
    ok = bad = skip = 0
    for name, r in results:
        if r is None:
            mark, skip = "⊘ 跳过", skip + 1
        elif r:
            mark, ok = "✓ 通过", ok + 1
        else:
            mark, bad = "✗ 失败", bad + 1
        print(f"    {mark}  {name}")
    print("─" * 74)
    print(f"    通过 {ok} · 失败 {bad} · 跳过 {skip}")
    print("═" * 74)

    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
