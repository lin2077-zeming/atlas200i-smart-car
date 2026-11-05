#!/bin/bash
# mic_input_probe.sh —— 验证「3.5mm 声卡 + 模拟麦克风」能否作为单设备全双工
#
# 适用场景：
#   · 领夹麦咪头（3.5mm）直插声卡 mic 输入
#   · 或接收器 3.5mm 输出 → 声卡 mic 输入
#
# 它做四件事：
#   1. 找出同时具备播放(pcmXp)与采集(pcmXc)的声卡（那就是"单设备全双工"候选）
#   2. 打印/设置 mixer（Mic 增益、Mic Boost）
#   3. 录 5 秒，算 RMS / 峰值 / 削顶比例，判定 mic 输入是否有信号
#   4. 在同一张卡上做 5 次「播放 + 采集」并发测试
#
# 用法：
#   bash mic_input_probe.sh
#   CARD=1 bash mic_input_probe.sh
#   DUR=5 bash mic_input_probe.sh
#
# ⚠️ 会改当前会话的 mixer 增益（不改系统配置）；会发 440Hz 测试音，先把音量调小。
set -u

cd "$(dirname "$0")" || exit 1

if [ -f ./_pyenv.sh ]; then
    # shellcheck disable=SC1091
    . ./_pyenv.sh
fi
PY="${CAR_PY:-python3}"

DUR="${DUR:-5}"
OUT="${OUT:-./mic_probe_out}"
CARD="${CARD:-}"
mkdir -p "$OUT" || exit 1

say()  { printf '%s\n' "$*"; }
hr()   { printf '%s\n' "------------------------------------------------------------"; }
ok()   { printf '  ✅ %s\n' "$*"; }
bad()  { printf '  ❌ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }

cleanup() {
    for p in $(jobs -p 2>/dev/null); do
        kill "$p" 2>/dev/null || true
    done
}
trap cleanup EXIT

say "============================================================"
say " 3.5mm 声卡 mic 输入 + 单设备全双工验证"
say "============================================================"
say

for c in arecord aplay timeout; do
    if ! command -v "$c" >/dev/null 2>&1; then
        bad "缺少命令：$c"
        exit 1
    fi
done

# ── 1. 设备列表 ─────────────────────────────────────────────
say "【1】声卡列表"
cat /proc/asound/cards 2>/dev/null || warn "读不到 /proc/asound/cards"
say "--- arecord -l（采集）---"
arecord -l 2>/dev/null || true
say "--- aplay -l（播放）---"
aplay -l 2>/dev/null || true
say
if command -v lsusb >/dev/null 2>&1; then
    say "--- lsusb -t（看速率：12M=全速，480M=高速）---"
    lsusb -t 2>/dev/null || true
    say
fi

# ── 2. 找同时有播放+采集的卡 ────────────────────────────────
# 输出：card号 播放设备号 采集设备号
find_duplex_cards() {
    local card n has_p has_c pdev cdev
    for card in /proc/asound/card[0-9]*; do
        [ -e "$card" ] || continue
        n=${card##*card}
        has_p=$(ls "$card"/pcm*p 2>/dev/null | head -n 1)
        has_c=$(ls "$card"/pcm*c 2>/dev/null | head -n 1)
        if [ -n "$has_p" ] && [ -n "$has_c" ]; then
            pdev=$(basename "$has_p"); pdev=${pdev#pcmC}; pdev=${pdev#*D}; pdev=${pdev%p}
            cdev=$(basename "$has_c"); cdev=${cdev#pcmC}; cdev=${cdev#*D}; cdev=${cdev%c}
            printf '%s %s %s\n' "$n" "$pdev" "$cdev"
        fi
    done
}

mapfile -t CANDIDATES < <(find_duplex_cards)
if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    bad "没有找到同时具备播放与采集的声卡。"
    warn "这说明你插的 3.5mm 声卡可能只有播放，没有采集；"
    warn "或者它被枚举成了两块独立的卡（那就是两块设备，不是单设备全双工）。"
    exit 1
fi

say "【2】同时有播放+采集的声卡候选（card / 播放设备 / 采集设备）"
printf '  %s\n' "${CANDIDATES[@]}"
say

if [ -n "$CARD" ]; then
    picked=""
    for row in "${CANDIDATES[@]}"; do
        set -- $row
        if [ "$1" = "$CARD" ]; then picked="$row"; break; fi
    done
    if [ -z "$picked" ]; then
        bad "CARD=$CARD 不在候选里"
        exit 1
    fi
else
    if [ "${#CANDIDATES[@]}" -gt 1 ]; then
        bad "有多张候选声卡，请用 CARD=<卡号> 指定"
        exit 1
    fi
    picked="${CANDIDATES[0]}"
fi
set -- $picked
CARD="$1"; PDEV="$2"; CDEV="$3"
say "【3】本次使用：card $CARD，播放设备 $PDEV，采集设备 $CDEV"
say

# ── 3. mixer ────────────────────────────────────────────────
say "【4】mixer 控件（重点看有没有 Mic / Mic Boost / Line）"
if command -v amixer >/dev/null 2>&1; then
    amixer -c "$CARD" scontrols 2>/dev/null || warn "amixer 读不到控件"
    amixer -c "$CARD" contents >"$OUT/amixer_before.txt" 2>/dev/null || true
    say
    say "  尝试把 Mic 增益拉到 100%、打开 Mic Boost（尽力而为）…"
    for ctl in "Mic Capture" "Mic" "Capture" "Mic Boost" "Boost"; do
        amixer -c "$CARD" sset "$ctl" 100% cap >/dev/null 2>&1 || true
        amixer -c "$CARD" sset "$ctl" 100% >/dev/null 2>&1 || true
        amixer -c "$CARD" sset "$ctl" unmute >/dev/null 2>&1 || true
    done
    say "  改前状态已存：$OUT/amixer_before.txt（要恢复用 alsamixer 手动调回）"
else
    warn "没有 amixer（可装 alsa-utils）；请手动用 alsamixer 把 Mic 拉到 100%"
fi
say

# ── 4. 录音 + RMS ───────────────────────────────────────────
say "【5】录音 ${DUR} 秒 —— 请对着麦克风正常说话（距离 15~25 cm）"
say "     现在开始…"
CAP_CH=""
CAP_RATE=""
for rate in 48000 44100; do
    for ch in 1 2; do
        say "  尝试 ${rate} Hz / ${ch} 声道 …"
        arecord -D "plughw:$CARD,$CDEV" -f S16_LE -r "$rate" -c "$ch" -d "$DUR" \
            -V mono "$OUT/mic.wav" 2>&1 | tee "$OUT/arecord.log"
        rc=${PIPESTATUS[0]}
        if [ "$rc" -eq 0 ]; then
            CAP_RATE="$rate"; CAP_CH="$ch"
            break 2
        fi
    done
done
if [ -z "$CAP_CH" ]; then
    bad "录音失败（48000/44100 × 单/双声道都打不开，见 $OUT/arecord.log）"
    tail -n 5 "$OUT/arecord.log" 2>/dev/null || true
    exit 1
fi
ok "录音完成：$OUT/mic.wav（${CAP_RATE} Hz，${CAP_CH} 声道）"

"$PY" - "$OUT/mic.wav" <<'PY'
import array, math, sys, wave

path = sys.argv[1]
with wave.open(path) as w:
    n = w.getnframes()
    ch = w.getnchannels()
    raw = w.readframes(n)
if not raw:
    print("  ❌ 录音文件为空")
    sys.exit(1)

a = array.array("h")
a.frombytes(raw)
if not a:
    print("  ❌ 没有采样点")
    sys.exit(1)

print("  声道数：%d，采样点：%d" % (ch, len(a) // max(1, ch)))
best = None
for idx in range(ch):
    data = a[idx::ch]
    if not data:
        continue
    peak = max(abs(x) for x in data) / 32768.0
    rms = math.sqrt(sum(x * x for x in data) / len(data)) / 32768.0
    clip = sum(1 for x in data if abs(x) >= 32767 * 0.98) / len(data)
    print("  声道 %d：RMS=%.4f  峰值=%.4f  削顶=%.2f%%" % (idx, rms, peak, clip * 100))
    if best is None or rms > best[0]:
        best = (rms, peak, clip)

if best is None:
    print("  ❌ 没有可用采样")
    sys.exit(1)
rms, peak, clip = best
if rms >= 0.01 and peak < 0.98:
    print("  判定  ：✅ 有信号，mic 输入可用")
elif rms < 0.003:
    print("  判定  ：❌ 几乎无信号（检查偏置/插头/增益）")
else:
    print("  判定  ：⚠️ 有信号但偏小或削顶，看下面的建议")
PY
say

# ── 5. 同卡并发 ─────────────────────────────────────────────
say "【6】同一张卡上的「播放 + 采集」并发测试（5 次）"
TONE="$OUT/tone_48000.wav"
"$PY" - "$TONE" <<'PY'
import math, struct, sys, wave

path = sys.argv[1]
sr, dur = 48000, 3.0
n = int(sr * dur)
buf = bytearray()
for i in range(n):
    buf += struct.pack("<h", int(0.2 * 32767 * math.sin(2 * math.pi * 440 * i / sr)))
with wave.open(path, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(bytes(buf))
PY

pass_n=0
for i in 1 2 3 4 5; do
    ( timeout 10 aplay -q -D "plughw:$CARD,$PDEV" "$TONE" \
        >"$OUT/play_$i.log" 2>&1 ) &
    pid=$!
    sleep 0.3
    timeout 10 arecord -q -D "plughw:$CARD,$CDEV" -f S16_LE -r "$CAP_RATE" -c "$CAP_CH" \
        -d 3 "$OUT/duplex_$i.wav" >"$OUT/cap_$i.log" 2>&1
    crc=$?
    wait "$pid"
    prc=$?
    if [ "$prc" -eq 0 ] && [ "$crc" -eq 0 ]; then
        ok "第 $i 次：播放 + 采集 同时成功"
        pass_n=$((pass_n + 1))
    else
        bad "第 $i 次：播放 rc=$prc / 采集 rc=$crc（见 $OUT/*_$i.log）"
    fi
done
say

# ── 6. dmesg ────────────────────────────────────────────────
say "【7】内核近期音频/带宽报错"
dmesg_out=$(dmesg 2>/dev/null | grep -iE "usb.*(bandwidth|enospc)|cannot submit|snd_usb" | tail -n 10)
if [ -n "$dmesg_out" ]; then
    printf '%s\n' "$dmesg_out"
else
    warn "dmesg 没有相关记录（可能需要 root）"
fi
say

# ── 7. 总结 ─────────────────────────────────────────────────
hr
say "结论："
if [ "$pass_n" -ge 4 ]; then
    ok "同卡并发 $pass_n/5 成功 —— 单设备全双工可行，不需要买新 USB 声卡"
else
    bad "同卡并发只有 $pass_n/5 成功 —— 这块板子的 xHCI 连单设备同时 IN+OUT 都限制"
    say "   → 选项：USB 会议全向麦（HS 优先）/ 网络音箱旁路 / M.2 转 USB 控制器"
fi
say
say "如果上面 RMS 判定是 ❌：先查声卡 mic 输入的偏置电压（2~5V）和 TRS/TRRS 匹配，"
say "再考虑换带 mic bias 的声卡或电池供电的领夹麦。"
say
say "详细日志：$OUT/"
exit 0
