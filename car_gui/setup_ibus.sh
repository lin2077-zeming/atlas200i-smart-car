#!/usr/bin/env bash
# ============================================================================
#  setup_ibus.sh —— 配置中文输入法（ibus + libpinyin），全程非交互
#
#  为什么需要它（2026-09-27 实测）：
#    板子出厂预装的是 **fcitx4**，但我们的界面是 PySide6 = **Qt6**，
#    而 Qt6 的 platforminputcontexts 里**只有 ibus 插件、没有 fcitx 插件**
#    （Ubuntu 22.04 只提供 fcitx-frontend-qt5）。
#    → 所以必须改用 ibus，否则 Qt 界面里永远打不出中文。
#
#  用法：
#      bash setup_ibus.sh          # 配置并启动
#      bash setup_ibus.sh --check  # 只体检，不修改
# ============================================================================
set -u

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

echo "═══ ibus 中文输入法配置 ═══"
echo

# ── ① 定位 DBus 会话地址（ibus 走 DBus，远程/SSH 下最容易缺这个）─────────
ME=$(id -u)
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    for p in $(pgrep -u "$ME" -x lxqt-session 2>/dev/null) \
             $(pgrep -u "$ME" -x openbox     2>/dev/null); do
        a=$(tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null |
            sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p' | head -1)
        if [ -n "$a" ]; then export DBUS_SESSION_BUS_ADDRESS="$a"; break; fi
    done
fi
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$ME/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$ME/bus"
fi
: "${DISPLAY:=:0}"
: "${XAUTHORITY:=$HOME/.Xauthority}"
export DISPLAY XAUTHORITY

echo "① 运行环境"
echo "   DISPLAY=$DISPLAY"
echo "   DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-（空 ⚠️）}"
echo "   XAUTHORITY=$XAUTHORITY"
echo

# ── ② 依赖体检 ──────────────────────────────────────────────────────────
echo "② 依赖体检"
missing=0
for b in ibus-daemon ibus; do
    if command -v "$b" >/dev/null 2>&1; then
        echo "   ✓ $b"
    else
        echo "   ✗ $b 缺失 → sudo apt install -y ibus ibus-libpinyin"
        missing=$((missing + 1))
    fi
done
if dpkg -s ibus-libpinyin >/dev/null 2>&1; then
    echo "   ✓ ibus-libpinyin（$(dpkg -s ibus-libpinyin | sed -n 's/^Version: //p')）"
else
    echo "   ✗ ibus-libpinyin 缺失 → sudo apt install -y ibus-libpinyin"
    missing=$((missing + 1))
fi
if gsettings list-schemas 2>/dev/null | grep -q "^org.freedesktop.ibus.general$"; then
    echo "   ✓ GSettings schema：org.freedesktop.ibus.general"
else
    echo "   ✗ 找不到 ibus 的 GSettings schema（配置将无法写入）"
    missing=$((missing + 1))
fi
echo

# ── ③ 确认 Qt6 只有 ibus 可用（本项目的关键事实，顺手自检一下）─────────
echo "③ Qt 输入法插件自检"
QT6_IC=""
for d in "$(python3 -c 'import PySide6,os;print(os.path.dirname(PySide6.__file__))' 2>/dev/null)/Qt/plugins/platforminputcontexts" \
         /usr/lib/aarch64-linux-gnu/qt6/plugins/platforminputcontexts; do
    [ -d "$d" ] && { QT6_IC="$d"; break; }
done
if [ -n "$QT6_IC" ]; then
    echo "   Qt6 插件目录：$QT6_IC"
    for so in "$QT6_IC"/*.so; do
        [ -e "$so" ] && echo "     - $(basename "$so")"
    done
    if ls "$QT6_IC" | grep -q ibus; then
        echo "   ✓ 有 ibus 插件 → Qt6 界面必须用 QT_IM_MODULE=ibus"
    else
        echo "   ✗ 没有 ibus 插件 → Qt6 界面无法输入中文（需换输入法方案）"
    fi
    if ls "$QT6_IC" | grep -q fcitx; then
        echo "   · 也有 fcitx 插件（罕见，通常只有 Qt5 才有）"
    else
        echo "   · 无 fcitx 插件（这是正常的：Ubuntu 22.04 只有 fcitx-frontend-qt5）"
    fi
else
    echo "   （未找到 Qt6 插件目录，跳过）"
fi
echo

if [ "$CHECK_ONLY" = "1" ]; then
    echo "--check 模式，不做修改。"
    exit 0
fi
[ "$missing" -gt 0 ] && { echo "依赖缺失，先安装后再跑本脚本。"; exit 1; }

# ── ④ 启动 daemon ───────────────────────────────────────────────────────
echo "④ 启动 ibus-daemon"
if pgrep -u "$ME" -x ibus-daemon >/dev/null 2>&1; then
    echo "   · 已在运行，重启以加载新配置"
    ibus-daemon -r -d -x >/dev/null 2>&1
else
    ibus-daemon -drx >/dev/null 2>&1
fi
sleep 3
pgrep -u "$ME" -x ibus-daemon >/dev/null 2>&1 \
    && echo "   ✓ ibus-daemon 已运行（PID $(pgrep -u "$ME" -x ibus-daemon | tr '\n' ' ')）" \
    || echo "   ✗ 启动失败，试试手动： ibus-daemon -drx"
echo

# ── ⑤ 配置中文引擎 ──────────────────────────────────────────────────────
echo "⑤ 配置中文引擎（libpinyin 智能拼音）"
gsettings set org.freedesktop.ibus.general preload-engines "['libpinyin']" \
    && echo "   ✓ preload-engines = ['libpinyin']"
gsettings set org.freedesktop.ibus.general engines-order "['libpinyin']" \
    && echo "   ✓ engines-order   = ['libpinyin']"
# 切换快捷键：Super+空格（与 fcitx 习惯一致），失败也不影响
gsettings set org.freedesktop.ibus.general.hotkey triggers "['<Super>space']" 2>/dev/null \
    && echo "   ✓ 切换快捷键 = Super+空格"
echo "   （若偏好 Ctrl+空格： gsettings set org.freedesktop.ibus.general.hotkey triggers \"['<Control>space']\"）"
echo

# ── ⑥ 重启并验证 ────────────────────────────────────────────────────────
echo "⑥ 重启 daemon 并验证"
ibus-daemon -r -d -x >/dev/null 2>&1
sleep 3
echo "   已启用引擎：$(ibus engine 2>&1 | head -1)"
if ibus list-engine 2>/dev/null | grep -qi "libpinyin"; then
    echo "   ✓ libpinyin 已注册到 ibus"
else
    echo "   ⚠️ list-engine 里没看到 libpinyin —— 可能是 daemon 缓存，"
    echo "      注销重登录后再跑： bash setup_ibus.sh --check"
fi
echo

# ── ⑦ 使用提示 ──────────────────────────────────────────────────────────
cat <<'EOF'
⑦ 怎么用
  · 启动 GUI：
        bash run_gui.sh
    （run_gui.sh 已自动注入 QT_IM_MODULE=ibus / XMODIFIERS=@im=ibus
      以及 DBUS_SESSION_BUS_ADDRESS —— SSH 远程启动时尤其重要）
  · 在指令框里按 Super+空格（或 Ctrl+空格）切到中文，拼音输入即可。
  · 若还是打不出中文，依次查：
        1) bash setup_ibus.sh --check          # 看哪一步红
        2) 环境变量： echo $QT_IM_MODULE $XMODIFIERS $DBUS_SESSION_BUS_ADDRESS
        3) Qt 插件加载： QT_DEBUG_PLUGINS=1 bash run_gui.sh 2>&1 | grep -i ibus
EOF
