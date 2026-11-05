#!/bin/bash
# ============================================================================
#  find_ip.sh —— 在**板子上**查自己的 IP（找 IP 最可靠的办法）
#
#  用法（在板子上直接跑）：
#      bash find_ip.sh
#      bash find_ip.sh --ssh       # 只输出 IPv4，便于脚本取值
#
#  为什么要有这个：IP 是路由器 DHCP 分的，会变。
#  在 PC 上扫描可能因 **ARP 缓存过期** 而漏掉板子（踩过这个坑），
#  而板子自己查出来的地址**一定是准的**。
# ============================================================================
set -u

ONLY_SSH=0
[ "${1:-}" = "--ssh" ] && ONLY_SSH=1

# ── 取所有 UP 状态的 IPv4（排除回环）────────────────────────────
# 输出形如：wlan0            UP             192.168.31.128/24␠  ← ★ 行尾带空格
mapfile -t LINES < <(ip -4 -br addr show 2>/dev/null | grep -v '^lo' | grep -i 'UP')

# ── 取一行的 "接口名 IPv4" ──────────────────────────────────────
# ⚠️ 这里**不能用 ${line##*[[:space:]]} 或 ${line##* }**：
#    实测在本板的 bash 5.1.16 里两者都返回**空串**（很反直觉）。
#    改用 `set -- $line` 让 shell 按空白分词，bash/sh 下都可靠。
#    出参：IF_NAME / IF_IP
split_line() {
    # shellcheck disable=SC2086
    set -- $1
    IF_NAME="${1:-}"
    IF_CIDR="${3:-}"
    IF_IP="${IF_CIDR%%/*}"
}

# ── 简要模式：只吐 IPv4（优先 wlan0 = WiFi 主用）────────────────
if [ "$ONLY_SSH" = "1" ]; then
    for want in wlan0 eth1; do
        for l in "${LINES[@]}"; do
            split_line "$l"
            if [ "$IF_NAME" = "$want" ] && [ -n "$IF_IP" ]; then
                echo "$IF_IP"; exit 0
            fi
        done
    done
    for l in "${LINES[@]}"; do
        split_line "$l"
        [ -n "$IF_IP" ] && { echo "$IF_IP"; exit 0; }
    done
    exit 1
fi

# ══ 完整信息 ════════════════════════════════════════════════════
echo "════════════════════════════════════════════════════════════"
echo " 小车网络信息 ($(hostname))"
echo "════════════════════════════════════════════════════════════"

echo
echo "── 接口 ────────────────────────────────────────────────────"
for l in "${LINES[@]}"; do
    split_line "$l"
    [ -z "$IF_IP" ] && continue
    mac=$(cat "/sys/class/net/$IF_NAME/address" 2>/dev/null)
    # 标注哪个是推荐走的
    case "$IF_NAME" in
        wlan0) note="← WiFi（主用）" ;;
        eth1)  note="网线（备用）" ;;
        *)     note="" ;;
    esac
    printf "  %-8s %-16s %-20s %s\n" "$IF_NAME" "$IF_IP" "$mac" "$note"
done

echo
echo "── 默认路由 ────────────────────────────────────────────────"
ip route 2>/dev/null | grep '^default' | sed 's/^/  /' || echo "  (无)"

echo
echo "── WiFi ────────────────────────────────────────────────────"
ssid=$(nmcli -t -f active,ssid dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}')
printf "  %-14s %s\n" "当前 SSID" "${ssid:-（未连）}"
gw=$(ip route 2>/dev/null | awk '/^default.*wlan0/{print $3; exit}')
printf "  %-14s %s\n" "WiFi 网关" "${gw:-—}"

# ── 推荐地址（优先 WiFi）───────────────────────────────────────
BEST=""
for want in wlan0 eth1; do
    for l in "${LINES[@]}"; do
        split_line "$l"
        if [ "$IF_NAME" = "$want" ] && [ -n "$IF_IP" ]; then
            BEST="$IF_IP"; break 2
        fi
    done
done
if [ -z "$BEST" ]; then
    for l in "${LINES[@]}"; do
        split_line "$l"
        [ -n "$IF_IP" ] && { BEST="$IF_IP"; break; }
    done
fi

echo
echo "════════════════════════════════════════════════════════════"
if [ -n "$BEST" ]; then
    echo " ★ 在电脑上连本机："
    echo
    echo "       ssh root@$BEST"
    echo
    echo "   （IP 由路由器 DHCP 分配，可能变化）"
    echo "     想固定下来 → 在路由器做 DHCP 保留，MAC:"
    for l in "${LINES[@]}"; do
        split_line "$l"
        [ "$IF_NAME" = "wlan0" ] && \
            echo "         ac:45:ef:a6:36:c3  (wlan0)" && break
    done
else
    echo " ✗ 没找到可用的 IPv4 地址（网络没连上？）"
    echo "   试试： nmcli device status   /   nmcli connection up A102"
fi
echo "════════════════════════════════════════════════════════════"
