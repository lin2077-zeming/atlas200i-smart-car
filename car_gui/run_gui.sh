#!/usr/bin/env bash
# ============================================================================
#  run_gui.sh —— 启动小车手动控制台
#
#  优先用 PySide6 版；装不上或加载失败就自动回退到 Tkinter 版。
#
#  用法：
#      bash run_gui.sh                      # 自动
#      bash run_gui.sh --port /dev/ttyCAR   # 指定串口
#      bash run_gui.sh --model-dir ~/models/sense-voice   # 指定语音模型
#      bash run_gui.sh --tk                 # 强制用 Tkinter 版
#      bash run_gui.sh --fullscreen         # PySide6 版全屏启动
# ============================================================================
set -u

cd "$(dirname "$0")"

# 统一解析解释器（本板多套 Python，依赖只装在其中一套里）
# shellcheck source=_pyenv.sh
. "$(dirname "$0")/_pyenv.sh"

FORCE_TK=0
PASSTHRU=()
for a in "$@"; do
    case "$a" in
        --tk) FORCE_TK=1 ;;
        *)    PASSTHRU+=("$a") ;;
    esac
done

echo "使用解释器：$CAR_PY"

# ── 语音依赖提示（不阻断启动）───────────────────────────────────────────
if ! "$CAR_PY" -c "import sherpa_onnx" 2>/dev/null; then
    echo "[提示] 未检测到 sherpa-onnx，语音面板将不可用（手动控制不受影响）。"
    echo "       要启用语音： bash install_deps.sh && bash download_asr_model.sh"
    echo "       若确认已装，多半是用错了 Python： python3 env_doctor.py"
fi
if ! "$CAR_PY" -c "import sounddevice" 2>/dev/null; then
    echo "[提示] 未检测到 sounddevice，无法录音。请运行： bash install_deps.sh"
    echo "       若确认已装，多半是用错了 Python： python3 env_doctor.py"
fi

# ── 显示环境检查（最常见的一类启动失败）─────────────────────────────────
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    cat <<'EOF'
[错误] 没有检测到图形显示环境（DISPLAY / WAYLAND_DISPLAY 都是空的）。

GUI 程序不能通过 SSH 直接运行。请任选一种方式：
  1) 把显示器接到开发板的 HDMI0 接口，在板子的本地终端里运行本脚本
  2) 用 VNC 连上开发板，在 VNC 桌面里打开终端运行
     若走 VNC，通常需要先手动指定：  export DISPLAY=:1
  3) 只是想在 SSH 里验证逻辑，可先用命令行版：
         python3 car_gui_core.py --port /dev/ttyCAR
EOF
    exit 1
fi

echo "DISPLAY=${DISPLAY:-}  WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-}"

# ── 中文输入法环境 ──────────────────────────────────────────────────────
# 为什么需要这段（2026-09-27 实测踩坑，别删）：
#
#   1) 本界面是 PySide6 = **Qt6**。Qt6 自带的 platforminputcontexts 里
#      **只有 ibus 插件，没有 fcitx 插件**（Ubuntu 22.04 只提供
#      fcitx-frontend-qt5）。所以 `QT_IM_MODULE=fcitx` 对 Qt6 无效 ——
#      板子预装的 fcitx4 打不出中文，这是根因。#      → 必须用 ibus（已装 ibus + ibus-libpinyin）。
#
#   2) 通过 SSH 远程启动 GUI 时，进程**继承不到桌面会话**的
#      QT_IM_MODULE / XMODIFIERS / GTK_IM_MODULE，必须在这里显式导出。
#
#   3) ibus 走 DBus，远程启动同样缺 DBUS_SESSION_BUS_ADDRESS。
#      → 从桌面会话进程（lxqt-session / openbox）的 environ 里抓。
setup_input_method() {
    # ① 补 DBus 会话地址（抓桌面会话的最可靠）
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
        for _p in $(pgrep -u "$(id -u)" -x lxqt-session 2>/dev/null) \
                  $(pgrep -u "$(id -u)" -x openbox     2>/dev/null); do
            _a=$(tr '\0' '\n' < "/proc/$_p/environ" 2>/dev/null |
                 sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p' | head -1)
            if [ -n "$_a" ]; then
                export DBUS_SESSION_BUS_ADDRESS="$_a"
                break
            fi
        done
    fi
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$(id -u)/bus" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
    fi

    # ② 选框架：Qt6 只认 ibus → 优先 ibus
    _im=""
    if command -v ibus-daemon >/dev/null 2>&1; then
        _im=ibus
        pgrep -u "$(id -u)" -x ibus-daemon >/dev/null 2>&1 || \
            ibus-daemon -drx >/dev/null 2>&1
    elif pgrep -u "$(id -u)" -x fcitx >/dev/null 2>&1; then
        _im=fcitx
        echo "[提示] 系统里只有 fcitx，但 PySide6(Qt6) **没有 fcitx 插件** →"
        echo "       中文输入仍会失效。请安装 ibus："
        echo "         sudo apt install -y ibus ibus-libpinyin"
    fi

    if [ -n "$_im" ]; then
        export QT_IM_MODULE="$_im" GTK_IM_MODULE="$_im" \
               XMODIFIERS="@im=$_im" QT4_IM_MODULE="$_im" CLUTTER_IM_MODULE="$_im"
        echo "输入法框架：$_im   DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-（空！）}"
        if [ "$_im" = ibus ] && command -v ibus >/dev/null 2>&1; then
            if ! ibus list-engine 2>/dev/null | grep -qi pinyin; then
                echo "[提示] ibus 里还没配中文引擎。执行："
                echo "         ibus-setup     # 图形界面 → Input Method → 添加 Chinese(Intelligent Pinyin)"
                echo "       或命令行："
                echo "         dconf write /org/freedesktop/ibus/general/preload-engines \"['libpinyin']\""
            fi
        fi
    else
        echo "[提示] 没检测到输入法框架，界面里将**无法输入中文**（可先用语音输入）。"
    fi

    # ③ locale 必须是 UTF-8，否则输入法不会激活
    case "${LC_CTYPE:-${LANG:-}}" in
        *UTF-8*|*utf8*) ;;
        *) export LANG=C.UTF-8 LC_CTYPE=C.UTF-8 ;;
    esac
}
setup_input_method

# ── 选择界面 ────────────────────────────────────────────────────────────
if [ "$FORCE_TK" = "0" ] && "$CAR_PY" -c "import PySide6, PySide6.QtWidgets" 2>/dev/null; then
    echo "使用 PySide6 界面：car_gui_pyside.py"
    exec "$CAR_PY" car_gui_pyside.py "${PASSTHRU[@]+"${PASSTHRU[@]}"}"
fi

if [ "$FORCE_TK" = "0" ]; then
    echo "[提示] 未检测到可用的 PySide6，自动回退到 Tkinter 界面。"
    echo "       如需安装：bash install_deps.sh"
fi

if ! "$CAR_PY" -c "import tkinter" 2>/dev/null; then
    echo "[错误] Tkinter 也不可用。请执行： sudo apt install -y python3-tk"
    exit 1
fi

echo "使用 Tkinter 界面：car_gui_tk.py"
exec "$CAR_PY" car_gui_tk.py "${PASSTHRU[@]+"${PASSTHRU[@]}"}"
