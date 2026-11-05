#!/usr/bin/env bash
# ============================================================================
#  display_supervisor.sh —— 展示屏看护进程（开机自启 / 免登录都走它）
#
#  为什么不是直接跑 run_display.sh：
#    1) 板子一上电就显示，**不用等 SSH，也不用等有人输密码登录桌面**
#       （配合 `setup_display_autostart.sh --boot`：sddm 自动登录 + 本脚本自启）
#    2) 主控 car_web 还在启动时先等一会儿，避免开屏先闪一下「与主控断开」
#    3) 展示屏**崩了 2 秒后自己回来**（方案 §7「崩了能自己回来」）
#    4) 用户按 Esc / F12 主动退出 = 想用桌面 → 退出码 0，不再拉起
#    5) 同一时刻只允许一个实例（flock），避免桌面自启被触发两次 → 两块界面抢屏
#
#  退出码：0 = 用户主动退出或已有实例；3 = 没有图形环境（不再空转）
#
#  手动用：
#      bash display_supervisor.sh                 # 看护模式（关掉再按 Esc 才停）
#      bash display_supervisor.sh --wait-web=0    # 不等主控，立刻显示
# ============================================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="${CAR_DISPLAY_LOG:-$HOME/.cache/car-display.log}"
CMD="${CAR_DISPLAY_CMD:-bash $HERE/run_display.sh}"
WAIT_WEB="${CAR_DISPLAY_WAIT_WEB:-20}"      # 等主控最长秒数
DELAY="${CAR_DISPLAY_DELAY:-0}"             # 起来先等几秒（等桌面/WM 就绪）
BACKOFF="${CAR_DISPLAY_BACKOFF:-2}"         # 每次崩溃后多等 2s×失败次数
MAXFAILS="${CAR_DISPLAY_MAXFAILS:-6}"       # 连续失败这么多次 → 慢速重试
LONG_SLEEP="${CAR_DISPLAY_LONG_SLEEP:-120}" # 慢速重试间隔
API="${CAR_WEB_BASE:-http://127.0.0.1:8090}/api/status"
LOCK="$HOME/.cache/car-display.lock"

# ── 命令行只认 --wait-web=N（其余参数原样透传给看护对象）──────────────
EXTRA=""
for a in "$@"; do
    case "$a" in
        --wait-web=*) WAIT_WEB="${a#*=}" ;;
        --delay=*)    DELAY="${a#*=}" ;;
        *) EXTRA="$EXTRA $a" ;;
    esac
done
[ -n "$EXTRA" ] && CMD="$CMD$EXTRA"

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG" 2>/dev/null || true; }

# 日志别把 eMMC 写满：超过 1 MB 只留最后 200 KB
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    tail -c 200000 "$LOG" >"$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi

# ── 单实例 ────────────────────────────────────────────────────────────
if command -v flock >/dev/null 2>&1; then
    mkdir -p "$(dirname "$LOCK")" 2>/dev/null || true
    if exec 9>"$LOCK" 2>/dev/null; then
        if ! flock -n 9 2>/dev/null; then
            log "已有展示屏实例在跑（锁 $LOCK），本次退出"
            exit 0
        fi
    else
        # 锁文件都建不出来（HOME 不可写）→ 跳过单实例检查，别误判成"已有实例"
        log "拿不到锁文件 $LOCK，跳过单实例检查"
    fi
fi

# ── 没有图形环境就没戏（别空转）──────────────────────────────────────
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
    log "没有 DISPLAY/WAYLAND_DISPLAY，展示屏无法显示（退出码 3）"
    echo "[提示] 没有图形环境，展示屏无法显示。"
    echo "       桌面/VNC 里跑： bash $HERE/run_display.sh"
    exit 3
fi

# ── 起来先等几秒：桌面刚登录时 WM/面板还没就绪，全屏窗口会抢不到焦点 ──
if [ "$DELAY" -gt 0 ] 2>/dev/null; then
    log "启动前等待 ${DELAY}s（等桌面就绪）"
    sleep "$DELAY"
fi

# ── 等主控起来（避免开屏就闪「与主控断开」）────────────────────────────
if [ "$WAIT_WEB" -gt 0 ] && command -v curl >/dev/null 2>&1; then
    t=0
    while ! curl -fsS -m 1 -o /dev/null "$API" 2>/dev/null; do
        if [ "$t" -ge "$WAIT_WEB" ]; then
            log "等主控 ${WAIT_WEB}s 超时，先起界面（稍后自己会连上）"
            break
        fi
        sleep 1
        t=$((t + 1))
    done
    [ "$t" -gt 0 ] && log "等主控 ${t}s 后开始显示"
fi

# ── 看护循环 ─────────────────────────────────────────────────────────
fails=0
while :; do
    started=$(date +%s)
    log "启动：$CMD"
    bash -c "$CMD"
    rc=$?
    ran=$(( $(date +%s) - started ))

    if [ "$rc" -eq 0 ]; then
        log "退出码 0（用户主动退出），看护结束"
        exit 0
    fi
    if [ "$rc" -eq 3 ]; then
        log "退出码 3（无图形环境），看护结束"
        exit 3
    fi

    # 正常跑过两分钟再崩 → 说明不是启动就坏，重新计数
    [ "$ran" -gt 120 ] && fails=1
    fails=$((fails + 1))
    if [ "$fails" -ge "$MAXFAILS" ]; then
        log "连续失败 ${fails} 次（rc=$rc，最后一次跑了 ${ran}s），${LONG_SLEEP}s 后再试"
        sleep "$LONG_SLEEP"
        fails=0
        continue
    fi
    wait_s=$(( fails * BACKOFF ))
    [ "$wait_s" -gt 20 ] && wait_s=20
    log "退出码 $rc（跑了 ${ran}s），${wait_s}s 后重启"
    sleep "$wait_s"
done