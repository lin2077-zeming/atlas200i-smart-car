#!/bin/bash
# audio_duplex_probe.sh —— 音频"同时录放"能力诊断（只读，不改任何系统配置）
#
# 目的：在买新声卡之前，先确定本板到底是
#   ① 两块 USB 音频设备抢同一份 USB 周期预算
#   ② 单块设备也不能同时录放（控制器限制）
#   ③ 某个采样率组合才能共存
#   ④ Hub 的单 TT 是瓶颈
#
# 用法：
#   bash audio_duplex_probe.sh
#   CAP_DEV=plughw:0,0 PLAY_DEV=plughw:1,0 bash audio_duplex_probe.sh
#   DUR=3 bash audio_duplex_probe.sh
#
# 输出：./audio_probe_out/ 下的日志与录音
#
# ⚠️ 会发 440Hz 测试音，先把音量旋钮调小；跑之前关掉控车 GUI。
set -u

cd "$(dirname "$0")" || exit 1

# 复用项目的解释器选择（CAR_PY）。只用到标准库 wave/math/struct。
if [ -f ./_pyenv.sh ]; then
    # shellcheck disable=SC1091
    . ./_pyenv.sh
fi
PY="${CAR_PY:-python3}"

DUR="${DUR:-2}"
OUT="${OUT:-./audio_probe_out}"
CAP_DEV="${CAP_DEV:-}"
PLAY_DEV="${PLAY_DEV:-}"
mkdir -p "$OUT" || exit 1

say()  { printf '%s\n' "$*"; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }
ok()   { printf '  ✅ %s\n' "$*"; }
bad()  { printf '  ❌ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }

# 只清理本脚本自己起的后台任务（绝不 pkill 别人的 aplay）
cleanup() {
    for p in $(jobs -p 2>/dev/null); do
        kill "$p" 2>/dev/null || true
    done
}
trap cleanup EXIT

# ── 0. 前置检查 ─────────────────────────────────────────────
say "============================================================"
say " 音频并发能力诊断（只读）"
say "============================================================"
say

if pgrep -f "car_gui_pyside|car_gui_tk" >/dev/null 2>&1; then
    warn "检测到控车 GUI 正在运行，它会占用麦克风/扬声器。"
    warn "请先关掉 GUI；脚本将在 5 秒后继续（Ctrl-C 退出）…"
    sleep 5
fi

for c in arecord aplay timeout; do
    if ! command -v "$c" >/dev/null 2>&1; then
        bad "缺少命令：$c"
        exit 1
    fi
done

# ── 1. 拓扑与设备 ───────────────────────────────────────────
say "【1】USB 拓扑（重点：音频设备在哪条 Bus、是 12M 还是 480M）"
if command -v lsusb >/dev/null 2>&1; then
    lsusb -t 2>/dev/null || lsusb 2>/dev/null
else
    warn "没有 lsusb（可装 usbutils）"
fi
say

say "【2】声卡列表"
say "--- /proc/asound/cards ---"
cat /proc/asound/cards 2>/dev/null || warn "读不到 /proc/asound/cards"
say "--- arecord -l（采集）---"
arecord -l 2>/dev/null || true
say "--- aplay -l（播放）---"
aplay -l 2>/dev/null || true
say

# ── 2. 自动选设备 ───────────────────────────────────────────
# arecord -l 的行格式：
#   card 0: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
# 用 POSIX awk 解析（不依赖 sed 的反向引用，Git Bash / mawk / gawk 都稳）
parse_first_dev() {
    awk '/^card [0-9]+:.*device [0-9]+:/ {
        c = $2; sub(/:$/, "", c)
        for (i = 1; i <= NF; i++) {
            if ($i == "device") {
                d = $(i + 1); sub(/:$/, "", d)
                print "plughw:" c "," d
                exit
            }
        }
    }'
}
detect_cap()  { arecord -l 2>/dev/null | parse_first_dev; }
detect_play() { aplay     -l 2>/dev/null | parse_first_dev; }
[ -n "$CAP_DEV" ]  || CAP_DEV="$(detect_cap)"
[ -n "$PLAY_DEV" ] || PLAY_DEV="$(detect_play)"

say "【3】本次使用的设备"
say "  采集：${CAP_DEV:-<未找到>}"
say "  播放：${PLAY_DEV:-<未找到>}"
if [ -z "$CAP_DEV" ] || [ -z "$PLAY_DEV" ]; then
    bad "缺少采集或播放设备，退出"
    exit 1
fi
say

# ── 3. 生成测试音 ───────────────────────────────────────────
say "【4】生成测试音（440Hz，${DUR}s）"
if ! "$PY" - "$OUT" "$DUR" <<'PY'
import math, struct, sys, wave

out, dur = sys.argv[1], float(sys.argv[2])
for sr in (16000, 22050, 44100, 48000):
    path = "%s/tone_%d.wav" % (out, sr)
    n = int(sr * dur)
    buf = bytearray()
    for i in range(n):
        v = int(0.25 * 32767 * math.sin(2 * math.pi * 440 * i / sr))
        buf += struct.pack("<h", v)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(buf))
    print("  %s" % path)
PY
then
    bad "生成测试音失败（解释器：$PY）"
    exit 1
fi
say

# ── 4. 单项基线 ─────────────────────────────────────────────
play_once() {  # $1=device $2=wav $3=tag
    timeout 12 aplay -q -D "$1" "$2" >"$OUT/play_$3.log" 2>&1
}
cap_once() {   # $1=device $2=rate $3=seconds $4=tag
    timeout 12 arecord -q -D "$1" -f S16_LE -r "$2" -c 1 -d "$3" \
        "$OUT/rec_$4.wav" >"$OUT/cap_$4.log" 2>&1
}

say "【5】单独播放（应全部成功）"
for sr in 22050 48000; do
    play_once "$PLAY_DEV" "$OUT/tone_$sr.wav" "solo_$sr"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        ok "播放 ${sr} Hz"
    else
        bad "播放 ${sr} Hz（rc=$rc，见 $OUT/play_solo_$sr.log）"
    fi
done
say

say "【6】单独采集（应全部成功；失败可能是增益/权限/设备选错）"
for sr in 16000 44100 48000; do
    cap_once "$CAP_DEV" "$sr" "$DUR" "solo_$sr"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        ok "采集 ${sr} Hz"
    else
        bad "采集 ${sr} Hz（rc=$rc，见 $OUT/cap_solo_$sr.log）"
    fi
done
say

# ── 5. 并发：先播后录（复现"播报时开麦失败"）────────────────
say "【7】并发：先播后录（复现「播报时开麦失败」）"
run_play_then_cap() {  # $1=play_sr $2=cap_sr $3=tag
    local prc crc pid
    ( timeout 12 aplay -q -D "$PLAY_DEV" "$OUT/tone_$1.wav" \
        >"$OUT/play_$3.log" 2>&1 ) &
    pid=$!
    sleep 0.4
    timeout 12 arecord -q -D "$CAP_DEV" -f S16_LE -r "$2" -c 1 -d "$DUR" \
        "$OUT/rec_$3.wav" >"$OUT/cap_$3.log" 2>&1
    crc=$?
    wait "$pid"
    prc=$?
    [ "$prc" -eq 0 ] && [ "$crc" -eq 0 ]
}
for pair in "22050 48000" "22050 16000" "48000 48000" "48000 16000"; do
    read -r psr csr <<< "$pair"
    tag="p${psr}_c${csr}"
    if run_play_then_cap "$psr" "$csr" "$tag"; then
        ok "播放 ${psr}Hz + 采集 ${csr}Hz 并行"
    else
        bad "播放 ${psr}Hz + 采集 ${csr}Hz 冲突（见 $OUT/*_${tag}.log）"
    fi
done
say

# ── 6. 并发：先录后播（复现"麦克风占着时播放失败"）──────────
say "【8】并发：先录后播（复现「麦克风占着时播放失败」）"
run_cap_then_play() {  # $1=cap_sr $2=play_sr $3=tag
    local prc crc pid
    ( timeout 12 arecord -q -D "$CAP_DEV" -f S16_LE -r "$1" -c 1 \
        -d "$((DUR + 1))" "$OUT/rec_$3.wav" >"$OUT/cap_$3.log" 2>&1 ) &
    pid=$!
    sleep 0.4
    timeout 12 aplay -q -D "$PLAY_DEV" "$OUT/tone_$2.wav" \
        >"$OUT/play_$3.log" 2>&1
    prc=$?
    wait "$pid"
    crc=$?
    [ "$prc" -eq 0 ] && [ "$crc" -eq 0 ]
}
for pair in "48000 22050" "16000 22050" "48000 48000" "16000 48000"; do
    read -r csr psr <<< "$pair"
    tag="c${csr}_p${psr}"
    if run_cap_then_play "$csr" "$psr" "$tag"; then
        ok "采集 ${csr}Hz + 播放 ${psr}Hz 并行"
    else
        bad "采集 ${csr}Hz + 播放 ${psr}Hz 冲突（见 $OUT/*_${tag}.log）"
    fi
done
say

# ── 7. 同卡全双工 ───────────────────────────────────────────
say "【9】同一张声卡的录放并行（若某卡同时有 pcmXp 与 pcmXc）"
found_duplex=0
for card in /proc/asound/card[0-9]*; do
    [ -e "$card" ] || continue
    n=$(basename "$card" | sed 's/^card//')
    has_p=$(ls "$card"/pcm*p 2>/dev/null | head -n 1)
    has_c=$(ls "$card"/pcm*c 2>/dev/null | head -n 1)
    if [ -n "$has_p" ] && [ -n "$has_c" ]; then
        found_duplex=1
        # 从 pcmC0D0p / pcmC0D0c 里取出设备号（用参数展开，避开 sed 反向引用）
        pdev=$(basename "$has_p"); pdev=${pdev#pcmC}; pdev=${pdev#*D}; pdev=${pdev%p}
        cdev=$(basename "$has_c"); cdev=${cdev#pcmC}; cdev=${cdev#*D}; cdev=${cdev%c}
        say "  card $n 同时有播放(D$pdev)与采集(D$cdev)，测试同卡全双工 …"
        ( timeout 12 aplay -q -D "plughw:$n,$pdev" "$OUT/tone_22050.wav" \
            >"$OUT/play_duplex_$n.log" 2>&1 ) &
        pid=$!
        sleep 0.4
        if timeout 12 arecord -q -D "plughw:$n,$cdev" -f S16_LE -r 48000 -c 1 \
            -d "$DUR" "$OUT/rec_duplex_$n.wav" \
            >"$OUT/cap_duplex_$n.log" 2>&1; then
            if wait "$pid"; then
                ok "card $n 同卡全双工可用"
            else
                bad "card $n 同卡全双工：采集成功但播放失败"
            fi
        else
            wait "$pid" || true
            bad "card $n 同卡全双工失败"
        fi
    fi
done
[ "$found_duplex" -eq 1 ] || warn "没有发现同时具备播放与采集的声卡"
say

# ── 8. 内核日志 ─────────────────────────────────────────────
say "【10】内核近期音频/带宽报错（-28 = ENOSPC）"
dmesg_out=$(dmesg 2>/dev/null | grep -iE "usb.*(bandwidth|enospc)|cannot submit|snd_usb" | tail -n 15)
if [ -n "$dmesg_out" ]; then
    printf '%s\n' "$dmesg_out"
else
    warn "dmesg 没有相关记录（可能需要 root，或本来就没有报错）"
fi
say

hr
say "结论怎么看："
say "  · 只有「单独」成功、所有「并发」都失败 → 两条流确实互斥，必须换通路"
say "  · 某个低采样率组合成功 → 优先把该组合做成软件默认，0 元解决"
say "  · 同卡全双工成功 → 买单设备全双工声卡即可（S1/S2）"
say "  · 全部失败且 dmesg 有 -28 → USB 周期预算 / hub TT 瓶颈，走单设备 HS 方案"
say
say "详细日志：$OUT/"
exit 0
