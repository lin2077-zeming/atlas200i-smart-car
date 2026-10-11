#!/bin/bash
# setup_mic_gain.sh —— 保证 USB 麦克风的录音增益不是 0
#
# 背景：2026-09-26 实测发现，C-Media/TI PCM2902 这颗免驱声卡
#       插上后默认 Mic Capture Volume 是 **0% (0.00dB)** —— 完全静音，
#       录音全是底噪，ASR 必然识别不出东西。
#
# 虽然 `alsactl store` 能把增益存进 /var/lib/alsa/asound.state，
# 但 USB 声卡是热插拔的，开机时枚举顺序变化可能导致恢复失效。
# 所以在 rc.local / systemd 里再兜底设一次，最稳。
#
# 用法：
#   bash setup_mic_gain.sh            # 立即设置并保存
#   bash setup_mic_gain.sh --install  # 额外安装为开机自启
#
set -u

# 统一解析解释器（本板多套 Python，依赖只装在其中一套里）
# shellcheck source=_pyenv.sh
. "$(dirname "$0")/_pyenv.sh"

GAIN="${MIC_GAIN:-100%}"
CARD="${MIC_CARD:-0}"

say() { printf '  %s\n' "$*"; }

find_mic_card() {
    # ★ 挑"最像麦克风"的声卡，**不是"第一个带采集的卡"**。
    #
    # 🔴 为什么必须这么改（2026-10-09 实测踩到）：
    #   本板同时插着两块 USB 音频设备时，声卡号会变：
    #       card 0 = 无线麦接收器（SPACETOUCH，采集+播放）
    #       card 1 = **有线麦 PCM2902（纯采集，in=1 out=0）**
    #   原来的逻辑"第一个带 pcm0c 的卡"会选中 **card 0**
    #   → **给错的卡设增益**，真正在用的有线麦增益仍是 0% → 录不到音。
    #   而且两块卡的枚举顺序**会随插拔变化**，靠卡号根本不可靠。
    #
    # 判据（与 asr_engine.score_input_device 一致）：
    #   **只有采集、没有播放**的卡才是真麦克风；
    #   采集=播放的"音箱卡"要排后面。
    #
    # 优先用 Python 侧的打分（单一事实来源，避免两处逻辑漂移）；
    # 拿不到就退回纯 shell 判据。
    if command -v "$CAR_PY" >/dev/null 2>&1 || [ -n "${CAR_PY:-}" ]; then
        n=$("$CAR_PY" - <<'PYEOF' 2>/dev/null
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))
                if "__file__" in dir() else ".")
try:
    sys.path.insert(0, "/home/HwHiAiUser/car_gui")
    import asr_engine as A
    d = A.pick_input_device(A.real_input_devices())
    if d is None:
        sys.exit(1)
    # 设备名尾部是 "(hw:N,M)" → 取出声卡号 N
    import re
    m = re.search(r"hw:(\d+),", d.get("name") or "")
    print(m.group(1) if m else "")
except Exception:
    sys.exit(1)
PYEOF
)
        [ -n "$n" ] && { echo "$n"; return 0; }
    fi

    # 退路：纯 shell —— 优先"只有采集没有播放"的卡
    local only_cap=""
    local any_cap=""
    for c in /proc/asound/card[0-9]*; do
        [ -d "$c" ] || continue
        local cn
        cn=$(basename "$c" | sed 's/card//')
        [ -f "$c/pcm0c/info" ] || continue
        [ -z "$any_cap" ] && any_cap="$cn"
        # 没有 pcm0p（播放端点）→ 纯采集 → 更像麦克风
        if [ ! -d "$c/pcm0p" ]; then
            only_cap="$cn"
            break
        fi
    done
    if [ -n "$only_cap" ]; then echo "$only_cap"; return 0; fi
    if [ -n "$any_cap" ]; then echo "$any_cap"; return 0; fi
    return 1
}

main() {
    echo "=============================================="
    echo " USB 麦克风增益设置"
    echo "=============================================="

    if ! [ -f /proc/asound/cards ] || grep -q 'no soundcards' /proc/asound/cards; then
        echo "  [错误] 板子还没识别到任何声卡。"
        say "请确认 USB 麦克风已插好，然后运行 arecord -l 复核。"
        return 1
    fi

    CARD="$(find_mic_card || echo "$CARD")"
    say "使用声卡：card $CARD"

    echo
    say "设置前的增益："
    amixer -c "$CARD" sget Mic 2>&1 | grep -E 'Mono|Limits' | sed 's/^/    /'

    echo
    say "设为 $GAIN ..."
    if amixer -c "$CARD" sset Mic "$GAIN" >/dev/null 2>&1; then
        say "OK"
    else
        echo "  [警告] 设置 Mic 增益失败，该声卡可能没有 Mic 控件"
    fi

    # AGC 对免驱小声卡很有用（说话远近不一）
    if amixer -c "$CARD" sset 'Auto Gain Control' on >/dev/null 2>&1; then
        say "Auto Gain Control 已开启"
    fi

    echo
    say "设置后的增益："
    amixer -c "$CARD" sget Mic 2>&1 | grep -E 'Mono' | sed 's/^/    /'

    echo
    say "保存到 /var/lib/alsa/asound.state ..."
    alsactl store "$CARD" 2>/dev/null && say "OK" || say "（跳过）"

    # ── 快速验证：录 2 秒看峰值 ──────────────────────────────
    # ⚠️ 三个坑都要避开：
    #    ① PCM2902 **硬件只支持 44100/48000**，用 -r 16000 会直接失败
    #    ② 板子桌面登录后 PulseAudio 可能抢占声卡（`Device or resource busy`）
    #    ③ **固定路径的临时文件会有权限冲突** —— 若之前用 root 跑过，
    #       `/tmp/_mic_check.wav` 属于 root，HwHiAiUser 覆盖不了会 Permission denied。
    #       而且旧文件非空，会让 `[ -s ]` 判断"假通过"。
    #       → 所以用**带 PID 的唯一文件名**，用完即删。
    echo
    say "录制 2 秒做验证（44100Hz，PCM2902 不支持 16k）..."
    RECTMP="/tmp/_mic_check_$$.wav"
    rm -f "$RECTMP" 2>/dev/null || true

    REC_OK=0
    REC_USED=""
    for devspec in "plughw:$CARD,0" "hw:$CARD,0" "default" "pulse"; do
        rm -f "$RECTMP" 2>/dev/null || true
        if arecord -D "$devspec" -d 2 -f S16_LE -r 44100 -c 1 \
                "$RECTMP" 2>/dev/null \
           && [ -s "$RECTMP" ]; then
            REC_OK=1
            REC_USED="$devspec"
            break
        fi
    done

    if [ "$REC_OK" = "1" ]; then
        echo "    使用设备：$REC_USED"
        MIC_WAV="$RECTMP" "$CAR_PY" - <<'PY'
import os
import wave
import numpy as np
p = os.environ["MIC_WAV"]
try:
    w = wave.open(p, 'rb')
    d = np.frombuffer(w.readframes(w.getnframes()),
                      dtype=np.int16).astype(np.float32) / 32768.0
    peak = float(np.abs(d).max())
    rms = float(np.sqrt((d ** 2).mean()))
    print("    峰值 = %.4f    RMS = %.4f" % (peak, rms))
    if peak < 0.01:
        print("    ⚠️ 仍然近乎静音 —— 请检查麦克风是否插在声卡的 Mic 孔（不是耳机孔）")
    elif peak > 0.98:
        print("    ⚠️ 可能过载削波，可把增益降到 80% 再试")
    else:
        print("    ✅ 电平正常")
except Exception as e:
    print("    分析失败：%s" % e)
PY
    else
        say "录音验证失败（增益设置本身已生效，只是没验成功）"
        cat <<'HINT'
    可能原因与对策：
      · PulseAudio 抢占了声卡 → 用下面任一方式绕开：
          fuser -v /dev/snd/*                              # 看谁占着
          env -u XDG_RUNTIME_DIR bash run_py.sh kws_record.py --diag
      · 声卡不在 card 0 → 先 arecord -l 确认，再 MIC_CARD=N bash setup_mic_gain.sh
      · 麦克风没插稳 → lsusb | grep 08bb
    注意：本步失败**不影响增益已经设成 100%** 这个事实。
HINT
    fi
    rm -f "$RECTMP" 2>/dev/null || true

    # ── 可选：安装成开机自启 ────────────────────────────────
    if [ "${1:-}" = "--install" ]; then
        echo
        say "安装开机自启 ..."
        SELF="$(readlink -f "$0")"
        UNIT=/etc/systemd/system/mic-gain.service
        cat > "$UNIT" <<EOF
[Unit]
Description=Set USB microphone capture gain (PCM2902 defaults to 0)
After=sound.target

[Service]
Type=oneshot
ExecStart=/bin/bash $SELF
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable mic-gain.service >/dev/null 2>&1
        say "已启用 mic-gain.service（下次开机会自动设置）"
        say "查看状态： systemctl status mic-gain"
    fi

    echo
    echo "=============================================="
    echo " 完成"
    echo "=============================================="
    return 0
}

main "$@"
