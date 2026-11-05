#!/usr/bin/env bash
# ============================================================================
#  setup_web_autostart.sh —— 让板子**开机自动启动**远程 Web 控制台
#
#  用法（板子上，需 root）：
#      bash setup_web_autostart.sh            # 安装并立即启动
#      bash setup_web_autostart.sh --status   # 看状态
#      bash setup_web_autostart.sh --uninstall
#
#  装完效果：板子一开机，`car_web.py` 就在后台跑着，
#            电脑上双击「启动小车控制台.cmd」会直接连上（不用再 SSH 启动）。
#
#  ⚠️ 安装时会顺手修两个**实测踩到的环境坑**，这是它能用起来的前提。
# ============================================================================
set -u

SERVICE="car-web"
UNIT="/etc/systemd/system/${SERVICE}.service"
GUI_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="HwHiAiUser"

if [ "$(id -u)" != "0" ]; then
    echo "请用 root 运行： sudo bash setup_web_autostart.sh"
    exit 1
fi

# ── 环境坑 ①：音频权限 ────────────────────────────────────────────────
#  实测（2026-10-27）：/dev/snd/pcmC0D0c 是 root:audio 660，
#  而 HwHiAiUser **不在 audio 组** —— 平时靠"桌面登录后 logind 临时给 ACL"
#  才能录音。一旦板子停在登录界面（没人登录），声卡就完全看不到，
#  表现为「检测不到任何录音设备」，语音输入/语音急停全废。
#  ⇒ 把用户永久加进 audio 组，不再依赖桌面会话。
fix_audio_group() {
    if id -nG "$RUN_USER" 2>/dev/null | tr ' ' '\n' | grep -qx "audio"; then
        echo "  [OK] $RUN_USER 已在 audio 组"
    else
        echo "  [修复] 把 $RUN_USER 加入 audio 组（否则麦克风不可见）"
        usermod -aG audio "$RUN_USER" && echo "         已加入（需重启服务生效）"
    fi
}

# ── 环境坑 ②：登录界面的 PulseAudio 独占声卡 ──────────────────────────
#  实测：pulseaudio 以 **sddm** 身份跑（登录界面会话），会 hold 住
#  /dev/snd/controlC0 → 我们进程用 ALSA 直连就打不开麦克风。
#  ⇒ 服务启动时自动释放它。
check_pulse() {
    if pgrep -u sddm -x pulseaudio >/dev/null 2>&1; then
        echo "  [注意] sddm 的 PulseAudio 正占着声卡；"
        echo "         服务的 ExecStartPre 会自动释放（否则麦克风打不开）"
    else
        echo "  [OK] 没有别的 PulseAudio 占着声卡"
    fi
}

do_status() {
    echo "=== $SERVICE 状态 ==="
    systemctl --no-pager status "$SERVICE" 2>/dev/null | head -14 || echo "未安装"
    echo
    echo "=== 端口 8090 ==="
    ss -tlnp 2>/dev/null | grep 8090 || echo "  没人监听 8090"
    echo
    echo "=== 音频权限 ==="
    id -nG "$RUN_USER" 2>/dev/null | tr ' ' '\n' | grep -x "audio" >/dev/null \
        && echo "  [OK] $RUN_USER 在 audio 组" || echo "  [缺] $RUN_USER 不在 audio 组"
}

do_uninstall() {
    echo "卸载 $SERVICE 服务…"
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "  已卸载（audio 组不会退回 —— 那是给麦克风用的，建议保留）"
}

do_install() {
    echo "════════════════════════════════════════════════"
    echo "  安装「开机自动启动 Web 控制台」"
    echo "════════════════════════════════════════════════"
    echo
    echo "[1/4] 检查运行环境"
    [ -f "$GUI_DIR/run_web.sh" ] || { echo "  ❌ 找不到 $GUI_DIR/run_web.sh"; exit 1; }
    [ -f "$GUI_DIR/car_web.py" ] || { echo "  ❌ 找不到 $GUI_DIR/car_web.py"; exit 1; }
    echo "  [OK] 脚本齐全：$GUI_DIR"
    fix_audio_group
    check_pulse

    echo
    echo "[2/4] 写 systemd 单元：$UNIT"
    cat > "$UNIT" <<EOF
[Unit]
Description=Car remote web console (car_web.py :8090)
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$GUI_DIR
Environment=HOME=/home/$RUN_USER
ExecStartPre=-/usr/bin/pkill -u sddm pulseaudio
# ★ 2026-10-28：TTS 把合成结果写成 wav 放进模型目录的 output/。只要有一次是
#   root 身份跑过（手工排障很常见），那些 wav 就归 root，之后服务（HwHiAiUser）
#   **覆盖不了** → 播报失败 "Permission denied: .../output/xxx.wav"。
#   这里以 root 身份（ExecStartPre 的 `+` 前缀）在每次启动时纠正属主。
ExecStartPre=+/bin/chown -R $RUN_USER:$RUN_USER /home/$RUN_USER/models/tts-npu/output
ExecStart=/bin/bash $GUI_DIR/run_web.sh
Restart=on-failure
RestartSec=5
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF
    echo "  [OK] 已写入"

    echo
    echo "[3/4] 启用并启动"
    systemctl daemon-reload
    systemctl enable "$SERVICE" >/dev/null 2>&1 && echo "  [OK] 已设为开机自启"
    systemctl restart "$SERVICE"
    sleep 3

    echo
    echo "[4/4] 校验"
    if systemctl is-active --quiet "$SERVICE"; then
        echo "  [OK] 服务在跑"
    else
        echo "  [失败] 服务没起来，最近日志："
        journalctl -u "$SERVICE" -n 25 --no-pager 2>/dev/null | sed 's/^/     /'
        exit 1
    fi
    local_ip=$(hostname -I | awk '{print $1}')
    echo
    echo "════════════════════════════════════════════════"
    echo "  完成！控制台地址： http://${local_ip}:8090/"
    echo "  电脑上双击「启动小车控制台.cmd」也会自动连上"
    echo "  看状态： bash setup_web_autostart.sh --status"
    echo "════════════════════════════════════════════════"
}

case "${1:-}" in
    --status)    do_status ;;
    --uninstall) do_uninstall ;;
    "")          do_install ;;
    *)           echo "用法：bash setup_web_autostart.sh [--status|--uninstall]"; exit 2 ;;
esac