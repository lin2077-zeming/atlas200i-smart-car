#!/usr/bin/env bash
# ============================================================================
#  run_display.sh —— 开发板展示屏启动器（只读大字界面）
#
#  用法（在**板子本地终端**或 VNC 桌面里运行；SSH 直接跑没有 DISPLAY 会失败）：
#      bash run_display.sh                      # 全屏演示
#      bash run_display.sh --windowed           # 窗口模式（调版式）
#      bash run_display.sh --preview            # 假数据预览（不连主控）
#      bash run_display.sh --screenshot a.png   # 渲染 PNG 后退出（**不需要显示器**）
#      bash run_display.sh --theme dark         # 暗环境主题
#      bash run_display.sh --scale 1.2          # 强制缩放（默认按屏高自适应）
#
#  启动后屏幕上**点任何地方都没有反应** —— 这是纯展示屏（方案 §11）。
#  唯一的键盘动作：**Esc / F12 = 退出回桌面**（退出码 0，看护进程不再拉起）。
#  所有操作留在电脑端浏览器控制台。
#
#  开机自动显示（免登录）：sudo bash setup_display_autostart.sh --boot
# ============================================================================
set -u
cd "$(dirname "$0")"

# ── 选解释器：本板多套 Python，只有部分装了 PySide6（见 _pyenv.sh）──────
PY=""
for c in "${CAR_PYTHON:-}" "$(head -1 .python_for_car 2>/dev/null | tr -d '\r\n ')" \
         /usr/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null)"; do
    [ -n "${c:-}" ] || continue
    [ -x "$c" ] || continue
    if "$c" -c 'import PySide6.QtWidgets' >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[错误] 找不到装了 PySide6 的 python3。"
    echo "       安装： bash install_deps.sh"
    echo "       或指定： CAR_PYTHON=/usr/bin/python3 bash run_display.sh"
    exit 1
fi

# ── 没有图形环境时给明确指引（SSH 直跑是最常见的一类失败）──────────────
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    cat <<'EOF'
[提示] 没有检测到图形显示环境（DISPLAY / WAYLAND_DISPLAY 都是空的）。

  展示屏是 Qt 程序，不能直接在 SSH 里全屏显示。三选一：
    1) 把显示器接到开发板 HDMI0 口，在**板子本地终端**里运行：
           bash run_display.sh
    2) 用 VNC 连上板子桌面，在桌面终端里运行：
           export DISPLAY=:1 && bash run_display.sh
    3) 只想看版式效果（**不需要显示器**，会存一张 PNG）：
           bash run_display.sh --screenshot ~/display.png

EOF
    case " $* " in
        *" --screenshot "*|*" --preview "*) : ;;   # 这两种可以离屏继续
        *) exit 3 ;;                              # 3 = 环境问题，看护进程别再拉起
    esac
fi

# ── ★ 必踩的坑 1：DPI 缩放会让 56 px 被放大 1.75 倍，版式全崩 ─────────
#    display.py 里也只用 setPixelSize()，这里把 Qt 的自动缩放彻底关掉。
export QT_ENABLE_HIGHDPI_SCALING=0
export QT_AUTO_SCREEN_SCALE_FACTOR=0
export QT_SCALE_FACTOR=1

# ── ★ 必踩的坑 2：屏保 / DPMS 会在演示中途黑屏 ────────────────────────
#   这里只是"起手先设一次"；真正的保险在 display.py 里 —— 板子的镜像会同时
#   跑 lxqt-powermanagement / xfce4-power-manager / xscreensaver，它们随时可能
#   把 DPMS 又打开，所以 display.py 每 20 s 重申一次（方案 §十五.10）。
#   注意顺序：`xset dpms force on` 会把 DPMS **重新打开**，所以：
#   先点亮 → 超时清零 → 最后禁 DPMS。
if [ -n "${DISPLAY:-}" ]; then
    xset dpms force on >/dev/null 2>&1 || true
    xset dpms 0 0 0 >/dev/null 2>&1 || true
    xset s off -dpms >/dev/null 2>&1 || true
    xset s noblank >/dev/null 2>&1 || true
fi

# ── 中文字体检查（缺了会显示一屏方块，方案 §8.2）──────────────────────
if command -v fc-list >/dev/null 2>&1; then
    if ! fc-list :lang=zh 2>/dev/null | grep -qi 'noto sans cjk\|droid sans fallback\|wenquanyi'; then
        echo "[警告] 没检测到中文字体，屏幕上可能出现方块。"
        echo "       安装： sudo apt install -y fonts-noto-cjk"
    fi
fi

echo "解释器：$PY   主控：${CAR_WEB_BASE:-http://127.0.0.1:8090}"
exec "$PY" display.py "$@"