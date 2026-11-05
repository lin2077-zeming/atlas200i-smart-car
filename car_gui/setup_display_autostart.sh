#!/usr/bin/env bash
# ============================================================================
#  setup_display_autostart.sh —— 让展示屏「一上电就有画面」
#
#  需求（2026-11-03）：只要电脑这边连上开发板，板子的屏上就该有展示界面；
#  不管小车自己有没有连上主控，也不管有没有人**输密码登录进桌面**。
#
#  四种装法，按"要不要登桌面"分：
#
#    --boot（**推荐**，需 root）
#        sddm 自动登录到 LXQt 桌面 + 登录后自启全屏展示屏。
#        效果：开机 ~20 s 屏上就是展示界面，全程不用输密码；
#        想看桌面按 Esc（展示屏退出，桌面露出来），想回来点桌面上的「小车展示屏」。
#        你需要改的只有一处：/etc/sddm.conf.d/50-car-autologin.conf
#        想恢复密码登录： sudo bash setup_display_autostart.sh --no-autologin
#
#    --boot-kiosk（需 root，演示机最省内存）
#        sddm 自动登录到一个**只有展示屏、没有桌面**的会话：
#        不开 LXQt/面板/文件管理器，省 200~300 MB 内存，开机也更快。
#        代价：板子上没有桌面可用（改代码/看文件请从电脑 SSH 过来）。
#
#    --desktop（默认，不需要 root）
#        只往当前用户的自启动目录放一个 .desktop：**登录桌面后**才自启。
#        保留密码登录界面，但没登录之前屏上是登录框，不是展示屏。
#
#    --xinit（进阶/兼容，需 root）
#        老办法：systemd 用 xinit 在 :1 上单独起一个 X。会和 sddm 抢显示，
#        只在"板子根本不装桌面"的机器上用，和 --boot 二选一。
#
#  其它：
#      --status        看当前状态（自动登录 / 自启 / 正在跑没有 / 图形会话 / 防息屏）
#      --apply         改完配置立刻重启 sddm 让它生效（会注销当前桌面！）
#      --no-autologin  关掉自动登录，恢复"输密码登录"（自启保留）
#      --uninstall     全卸掉（四种装法都清）
#      --print-config  只打印要写进去的文件内容，不落盘（离线自检用）
#
#  ★ 防息屏（2026-11-03 追加，用户报的"长期不动会黑屏、web 下指令也唤不醒"）：
#    板子镜像里 LXQt 电源管理 + XFCE 电源管理 + xscreensaver 会同时自启，
#    空闲几分钟就黑屏；而电脑端发指令走 HTTP，不是 X 输入事件，唤不醒屏保。
#    两道保险：① 装自启时在 ~/.config/autostart/ 把这些守护进程 Hidden=true 挡掉；
#              ② display.py 每 20 s 重申 `xset s off -dpms`，收到新指令/事件立刻
#                 `xset dpms force on` 把屏点亮。
# ============================================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/run_display.sh"
SUP="$HERE/display_supervisor.sh"
AUTOLOGIN_CONF="/etc/sddm.conf.d/50-car-autologin.conf"
KIOSK_SESSION="/usr/share/xsessions/car-display.desktop"
XINIT_UNIT="/etc/systemd/system/car-display.service"

MODE="desktop"
APPLY=0
for a in "$@"; do
    case "$a" in
        --boot)         MODE="boot" ;;
        --boot-kiosk)   MODE="boot-kiosk" ;;
        --desktop)      MODE="desktop" ;;
        --xinit)        MODE="xinit" ;;
        --no-autologin) MODE="no-autologin" ;;
        --status)       MODE="status" ;;
        --uninstall)    MODE="uninstall" ;;
        --print-config) MODE="print-config" ;;
        --apply)        APPLY=1 ;;
        -h|--help)      sed -n '2,45p' "$0"; exit 0 ;;
        *) echo "未知参数：$a（用 --help 看用法）"; exit 2 ;;
    esac
done

# ── 目标用户（sudo 进来时用 sudo 的那个人，别装到 /root 去）─────────────
TARGET_USER="${CAR_DISPLAY_USER:-}"
if [ -z "$TARGET_USER" ]; then
    if [ "$(id -u)" -eq 0 ]; then
        TARGET_USER="${SUDO_USER:-HwHiAiUser}"
    else
        TARGET_USER="$(id -un)"
    fi
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" 2>/dev/null | cut -d: -f6)"
[ -n "${TARGET_HOME:-}" ] || TARGET_HOME="${HOME:-/home/$TARGET_USER}"
TARGET_GROUP="$(id -gn "$TARGET_USER" 2>/dev/null || echo "$TARGET_USER")"

# 桌面目录可能是本地化的（中文系统里不叫 ~/Desktop 而叫 ~/桌面）
DESKTOP_DIR="$TARGET_HOME/Desktop"
_ud="$TARGET_HOME/.config/user-dirs.dirs"
if [ -f "$_ud" ]; then
    _d="$(sed -n 's/^XDG_DESKTOP_DIR="\$HOME\/\(.*\)"$/\1/p' "$_ud" 2>/dev/null | head -1)"
    if [ -n "${_d:-}" ] && [ -d "$TARGET_HOME/$_d" ]; then
        DESKTOP_DIR="$TARGET_HOME/$_d"
    fi
fi

AUTOSTART_FILE="$TARGET_HOME/.config/autostart/car-display.desktop"
DESKTOP_ICON="$DESKTOP_DIR/小车展示屏.desktop"

# ★ 防息屏：板子的镜像里同时装了 LXQt 电源管理、XFCE 电源管理、xscreensaver，
#   开机全会自启，抢着在空闲几分钟后黑屏；而电脑端发指令不是 X 输入，叫不醒。
#   做法：在用户级 autostart 里用同名 Hidden=true 把它们挡掉（XDG 规范：用户目录
#   的同名条目覆盖 /etc/xdg/autostart 的），这是"根源"；display.py 里每 20 s
#   重申 xset 是"兜底"。
NOBLANK_DAEMONS="lxqt-powermanagement lxqt-xscreensaver-autostart xscreensaver xfce4-power-manager xfce4-screensaver"

is_root() { [ "$(id -u)" -eq 0 ]; }
need_root() {
    is_root && return 0
    echo "[需要 root] 这一步要写系统配置，请用： sudo bash $0 ${1:---boot}"
    return 1
}
fix_owner() {  # fix_owner <path> <mode>
    chmod "${2:-0644}" "$1" 2>/dev/null || true
    if is_root; then
        chown "$TARGET_USER:$TARGET_GROUP" "$1" 2>/dev/null || true
    fi
}

# ── 要生成的文件内容（--print-config 和真装都走这里，保证一模一样）────
autologin_conf() {  # autologin_conf <session>
    cat <<EOF
# 由 car_gui/setup_display_autostart.sh --boot 生成（2026-11-03）
# 作用：开机自动登录，屏上直接出展示界面，不用走到板子前输密码。
# 恢复密码登录： sudo bash $HERE/setup_display_autostart.sh --no-autologin
[Autologin]
User=$TARGET_USER
Session=$1
Relogin=true
EOF
}

autostart_entry() {
    cat <<EOF
[Desktop Entry]
Type=Application
Name=小车展示屏
Comment=开发板展示屏（只读大字界面，按 Esc 回桌面）
Exec=/bin/bash $SUP --delay=3
Terminal=false
X-GNOME-Autostart-enabled=true
X-LXQt-Need-Tray=false
EOF
}

desktop_icon_entry() {
    cat <<EOF
[Desktop Entry]
Type=Application
Name=小车展示屏
Comment=全屏打开展示屏（Esc 退出）
Exec=/bin/bash $SUP
Icon=video-display
Terminal=false
EOF
}

kiosk_session_entry() {
    cat <<EOF
[Desktop Entry]
Type=Application
Name=小车展示屏
Comment=只显示展示屏，不加载桌面（省内存）
Exec=/bin/bash $SUP
TryExec=/bin/bash
DesktopNames=CarDisplay
EOF
}

noblank_entry() {  # noblank_entry <守护进程名>
    cat <<EOF
[Desktop Entry]
Type=Application
Name=$1（展示屏已禁用：防息屏）
Comment=展示屏画面长期不动，不能被屏保/省电黑屏
Hidden=true
X-GNOME-Autostart-enabled=false
EOF
}

xinit_unit() {
    cat <<EOF
[Unit]
Description=小车展示屏（xinit :1，无桌面）
After=network.target car-web.service
Wants=car-web.service

[Service]
Type=simple
User=$TARGET_USER
Environment=HOME=$TARGET_HOME
Environment=QT_ENABLE_HIGHDPI_SCALING=0
Environment=QT_AUTO_SCREEN_SCALE_FACTOR=0
ExecStart=/usr/bin/xinit /bin/bash $SUP -- :1
Restart=on-failure
RestartSec=5
KillMode=control-group

[Install]
WantedBy=graphical.target
EOF
}

# ── 状态 ─────────────────────────────────────────────────────────────
show_status() {
    echo "── 展示屏自启状态 ──────────────────────────────────"
    if [ -f "$AUTOLOGIN_CONF" ]; then
        echo "自动登录 ： 已装"
        sed -n 's/^User=/  用户   ： /p;s/^Session=/  会话   ： /p' "$AUTOLOGIN_CONF"
    else
        echo "自动登录 ： 未装（开机停在 sddm 登录框）"
    fi
    [ -f "$AUTOSTART_FILE" ] && echo "桌面自启 ： 已装  ($AUTOSTART_FILE)" \
                             || echo "桌面自启 ： 未装"
    [ -f "$DESKTOP_ICON" ]   && echo "桌面图标 ： 已装  ($DESKTOP_ICON)" \
                             || echo "桌面图标 ： 未装"
    [ -f "$KIOSK_SESSION" ]  && echo "kiosk会话： 已装  ($KIOSK_SESSION)" \
                             || echo "kiosk会话： 未装"
    [ -f "$XINIT_UNIT" ]     && echo "xinit服务： 已装（与自动登录二选一）" \
                             || echo "xinit服务： 未装"
    _nb=0
    for _n in $NOBLANK_DAEMONS; do
        [ -f "$TARGET_HOME/.config/autostart/$_n.desktop" ] && _nb=$((_nb + 1))
    done
    if [ "$_nb" -gt 0 ]; then
        echo "防息屏   ： 已装（挡掉 $_nb 个省电/屏保守护进程 + display.py 每 20 s 重申 xset）"
    else
        echo "防息屏   ： 未装（开机后空闲几分钟可能会黑屏）"
    fi
    echo "───────────────────────────────────────────────────"
    echo "正在跑   ： $(pgrep -af 'display_supervisor.sh|display.py' 2>/dev/null | head -3 | sed 's/^/           /' || true)"
    [ -z "$(pgrep -f 'display_supervisor.sh|display.py' 2>/dev/null)" ] && echo "           （没有）"
    if command -v loginctl >/dev/null 2>&1; then
        echo "图形会话 ："
        loginctl list-sessions --no-legend 2>/dev/null | awk '{printf "           %s %s %s\n", $1, $3, $4}' || true
    fi
    echo "手动启动 ： bash $RUN        （Esc 退出后想回来，点桌面图标也行）"
    echo "离屏预览 ： bash $RUN --screenshot /tmp/display.png"
}

# ── 安装动作 ─────────────────────────────────────────────────────────
install_noblank() {
    # 挡掉省电/屏保守护进程（用户级 Hidden=true 覆盖 /etc/xdg/autostart 的同名条目）
    mkdir -p "$TARGET_HOME/.config/autostart"
    for _n in $NOBLANK_DAEMONS; do
        noblank_entry "$_n" >"$TARGET_HOME/.config/autostart/$_n.desktop"
        fix_owner "$TARGET_HOME/.config/autostart/$_n.desktop" 0644
    done
    echo "✅ 防息屏：已挡掉 $NOBLANK_DAEMONS"
    echo "   （展示屏画面长期不动，不能被屏保/省电黑屏；电脑端发指令不是 X 输入，叫不醒）"
}

install_autostart() {
    mkdir -p "$TARGET_HOME/.config/autostart"
    autostart_entry >"$AUTOSTART_FILE" && fix_owner "$AUTOSTART_FILE" 0644
    mkdir -p "$DESKTOP_DIR"
    desktop_icon_entry >"$DESKTOP_ICON" && fix_owner "$DESKTOP_ICON" 0755
    echo "✅ 桌面自启：$AUTOSTART_FILE"
    echo "✅ 桌面图标：$DESKTOP_ICON"
    install_noblank
}

install_autologin() {  # install_autologin <session>
    need_root "$MODE" || return 1
    mkdir -p /etc/sddm.conf.d
    autologin_conf "$1" >"$AUTOLOGIN_CONF" && chmod 0644 "$AUTOLOGIN_CONF"
    echo "✅ 自动登录：$AUTOLOGIN_CONF（User=$TARGET_USER，Session=$1，Relogin=true）"
}

install_kiosk_session() {
    need_root "--boot-kiosk" || return 1
    kiosk_session_entry >"$KIOSK_SESSION" && chmod 0644 "$KIOSK_SESSION"
    echo "✅ 会话入口：$KIOSK_SESSION（sddm 里叫 car-display）"
}

restart_dm() {
    if [ "$APPLY" -eq 1 ] && is_root; then
        echo "→ 重启 sddm 让配置生效（当前桌面会话会被注销）…"
        systemctl restart sddm && echo "✅ 已重启 sddm" || echo "⚠️ 重启 sddm 失败，看： journalctl -u sddm -n 50"
    else
        echo "   现在生效： sudo systemctl restart sddm      # 会注销当前桌面！"
        echo "   （或者直接重启板子： sudo reboot）"
    fi
}

install_autostart_only() {
    install_autostart
    echo "   下次**登录桌面**会自动全屏启动；现在想立刻看效果："
    echo "       bash $RUN"
}

# ── 主流程 ───────────────────────────────────────────────────────────
if [ "$MODE" = "print-config" ]; then
    SESS="lxqt"
    [ -f "$KIOSK_SESSION" ] && SESS="car-display"
    echo "--- $AUTOLOGIN_CONF"
    autologin_conf "$SESS"
    echo "--- $AUTOSTART_FILE"
    autostart_entry
    echo "--- $DESKTOP_ICON"
    desktop_icon_entry
    echo "--- $KIOSK_SESSION"
    kiosk_session_entry
    echo "--- $TARGET_HOME/.config/autostart/xscreensaver.desktop   （防息屏，同款还有其余 N 个）"
    echo "# 挡掉的守护进程：$NOBLANK_DAEMONS"
    noblank_entry xscreensaver
    echo "--- $XINIT_UNIT"
    xinit_unit
    exit 0
fi

if [ "$MODE" = "status" ]; then show_status; exit 0; fi

if [ "$MODE" = "uninstall" ]; then
    rm -f "$AUTOSTART_FILE" "$DESKTOP_ICON" "$KIOSK_SESSION" && echo "已删除桌面自启/图标/kiosk 会话"
    for _n in $NOBLANK_DAEMONS; do
        rm -f "$TARGET_HOME/.config/autostart/$_n.desktop"
    done
    echo "已删除防息屏覆盖（省电/屏保守护进程会在下次登录恢复自启）"
    if [ -f "$AUTOLOGIN_CONF" ]; then
        need_root "--uninstall" && rm -f "$AUTOLOGIN_CONF" && echo "已删除自动登录配置"
    fi
    if [ -f "$XINIT_UNIT" ]; then
        need_root "--uninstall" && {
            systemctl disable --now car-display 2>/dev/null || true
            rm -f "$XINIT_UNIT"
            systemctl daemon-reload
            echo "已删除 xinit 服务"
        }
    fi
    echo "   恢复密码登录生效： sudo systemctl restart sddm"
    exit 0
fi

if [ "$MODE" = "no-autologin" ]; then
    need_root "--no-autologin" || exit 1
    if [ -f "$AUTOLOGIN_CONF" ]; then
        rm -f "$AUTOLOGIN_CONF"
        echo "✅ 已关掉自动登录（开机回到输密码登录界面）"
        echo "   桌面上登录后仍会自动全屏启动展示屏。"
    else
        echo "自动登录本来就没装。"
    fi
    restart_dm
    exit 0
fi

[ -f "$RUN" ] || { echo "[错误] 找不到 $RUN"; exit 1; }
[ -f "$SUP" ] || { echo "[错误] 找不到 $SUP"; exit 1; }

# 主控（car_web）得开机就起，展示屏才有数据可显示
if [ "$MODE" = "boot" ] || [ "$MODE" = "boot-kiosk" ]; then
    if command -v systemctl >/dev/null 2>&1 && ! systemctl is-enabled car-web >/dev/null 2>&1; then
        echo "⚠️ car-web.service 没设开机自启，展示屏会一直显示「等待控制台连接」"
        echo "   建议： sudo systemctl enable --now car-web"
    fi
fi

case "$MODE" in
    desktop)
        install_autostart_only
        ;;
    boot)
        DM="$(basename "$(readlink -f /etc/systemd/system/display-manager.service 2>/dev/null)" .service 2>/dev/null || true)"
        if [ -n "${DM:-}" ] && [ "$DM" != "sddm" ]; then
            echo "[跳过自动登录] 本机显示管理器是 $DM，不是 sddm。"
            echo "               先按通用方式装桌面自启，自动登录请告诉我用的哪个 DM。"
            install_autostart_only
            exit 0
        fi
        install_autologin lxqt || exit 1
        install_autostart
        echo "✅ 已装好：开机自动登录 → 全屏展示屏（不碰小车是否连接）"
        echo "   要回桌面：在展示屏上按 Esc；想再打开：桌面图标「小车展示屏」"
        echo "   要恢复密码登录： sudo bash $0 --no-autologin"
        restart_dm
        ;;
    boot-kiosk)
        install_kiosk_session || exit 1
        install_autologin car-display || exit 1
        echo "✅ 已装好：开机自动登录到「只有展示屏」的会话（无桌面，省内存）"
        echo "   板上没有桌面可用；要看桌面： sudo bash $0 --boot"
        restart_dm
        ;;
    xinit)
        need_root "--xinit" || exit 1
        xinit_unit >"$XINIT_UNIT" && chmod 0644 "$XINIT_UNIT"
        systemctl daemon-reload
        systemctl enable car-display >/dev/null 2>&1
        echo "✅ 已装 xinit 服务：$XINIT_UNIT"
        echo "   启动： sudo systemctl start car-display"
        echo "   ⚠️ 和 sddm 自动登录二选一，两者同时开会抢显示"
        ;;
    *)
        echo "未知模式：$MODE"; exit 2 ;;
esac