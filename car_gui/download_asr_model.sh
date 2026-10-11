#!/usr/bin/env bash
# ============================================================================
#  download_asr_model.sh —— 下载 SenseVoice 离线识别模型（约 156 MB）
#
#  产出目录（默认 ~/models/sense-voice/）：
#      model.int8.onnx      约 228 MB  ← 解压后大小
#      tokens.txt           约 308 KB
#      test_wavs/*.wav      官方测试音频（几个，用于离线验证识别是否正常）
#      silero_vad.onnx      约 0.6 MB  ← 用于自动断句，可选但强烈建议
#
#  用法：
#      bash download_asr_model.sh                       # 自动选源，装到 ~/models/sense-voice
#      bash download_asr_model.sh --dest /data/models/sense-voice
#      bash download_asr_model.sh --via ghfast          # 强制用某个加速源
#      bash download_asr_model.sh --keep-tarball        # 保留下载的压缩包（便于拷给板子）
#      bash download_asr_model.sh --force               # 已存在也重新下载
#
#  提示：板子直连下载慢时，**建议在 PC 上先跑本脚本**（Git Bash 里能跑），
#        再把整个目录传到板子，见《6.SSH连接与文件传输.md》。
# ============================================================================
set -u

# 统一解析解释器（本板多套 Python，依赖只装在其中一套里）
# shellcheck source=_pyenv.sh
. "$(dirname "$0")/_pyenv.sh"

DEST="$HOME/models/sense-voice"
VIA="auto"
FORCE=0
KEEP_TARBALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dest) DEST="$2"; shift 2 ;;
        --via)  VIA="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --keep-tarball) KEEP_TARBALL=1; shift ;;
        -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
        *) echo "未知参数：$1"; exit 1 ;;
    esac
done

C_OK="\033[32m"; C_WARN="\033[33m"; C_ERR="\033[31m"; C_OFF="\033[0m"
say()  { printf "\n${C_OK}==> %s${C_OFF}\n" "$1"; }
warn() { printf "${C_WARN}[警告] %s${C_OFF}\n" "$1"; }
err()  { printf "${C_ERR}[错误] %s${C_OFF}\n" "$1"; }

MODEL_NAME="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
TARBALL="${MODEL_NAME}.tar.bz2"
RAW_BASE="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
SILERO_URL="${RAW_BASE}/silero_vad.onnx"

# 至少需要这两个文件齐全才算装好
need_download() {
    [ "$FORCE" = "1" ] && return 0
    [ -f "$DEST/model.int8.onnx" ] && [ -f "$DEST/tokens.txt" ] && return 1
    return 0
}

# ── 0. 前置检查 ─────────────────────────────────────────────────────────
say "0/5 环境检查"
command -v curl >/dev/null 2>&1 || { err "缺少 curl"; exit 1; }
echo "  curl   : $(curl --version | head -1)"
echo "  目标目录: $DEST"
mkdir -p "$DEST" || { err "无法创建目录 $DEST"; exit 1; }

if ! need_download; then
    echo
    echo "  模型已存在，跳过下载。"
    echo "  如需重新下载，加 --force 参数。"
    ls -lh "$DEST" | sed 's/^/    /'
    exit 0
fi

# 记录一下剩余空间，228MB 需要余量
AVAIL_KB=$(df -Pk "$DEST" | tail -1 | awk '{print $4}')
AVAIL_MB=$((AVAIL_KB / 1024))
echo "  可用空间: ${AVAIL_MB} MB"
if [ "$AVAIL_MB" -lt 400 ]; then
    err "空间不足（需要约 400MB，当前 ${AVAIL_MB}MB）。"
    echo "  建议先挂载 NVMe 到 /data，再用 --dest /data/models/sense-voice"
    exit 1
fi

# ── 1. 尝试解压工具 ─────────────────────────────────────────────────────
TMP="$(mktemp -d 2>/dev/null || echo /tmp/asr_dl_$$)"
mkdir -p "$TMP"
cleanup() {
    if [ "$KEEP_TARBALL" = "1" ] && [ -f "$TMP/$TARBALL" ]; then
        cp -f "$TMP/$TARBALL" "$DEST/" 2>/dev/null || true
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT

# ── 2. 选择下载源 ───────────────────────────────────────────────────────
mirror_prefix() {
    case "$1" in
        github) echo "" ;;
        ghfast) echo "https://ghfast.top/" ;;
        gh-proxy) echo "https://gh-proxy.com/" ;;
        ghproxy) echo "https://ghproxy.net/" ;;
        *) echo "" ;;
    esac
}

if [ "$VIA" = "auto" ]; then
    SOURCES="github ghfast gh-proxy ghproxy"
else
    SOURCES="$VIA"
fi

dl() {
    # dl <url> <输出文件>
    local url="$1" out="$2"
    echo "  下载：$url"
    curl -L --fail --retry 3 --retry-delay 2 --connect-timeout 20 \
         -C - -o "$out" "$url"
}

say "1/5 下载模型压缩包（约 156 MB，视网速可能要几分钟）"
OK=0
for src in $SOURCES; do
    PREFIX="$(mirror_prefix "$src")"
    URL="${PREFIX}${RAW_BASE}/${TARBALL}"
    echo
    echo "  -- 尝试源：$src"
    if dl "$URL" "$TMP/$TARBALL"; then
        # 校验大小（正常应在 150MB 以上；太小说明下到的是错误页面）
        SIZE=$(wc -c < "$TMP/$TARBALL" 2>/dev/null || echo 0)
        if [ "$SIZE" -gt 100000000 ]; then
            OK=1
            echo "  成功，大小 $((SIZE / 1048576)) MB"
            break
        else
            warn "文件只有 $((SIZE / 1024)) KB，可能不是真正的模型包，换下一个源"
        fi
    else
        warn "该源失败，换下一个"
    fi
done

if [ "$OK" != "1" ]; then
    err "所有下载源都失败了。"
    cat <<EOF

  可选做法：
  1) 在 PC 上下载（Git Bash 里执行，或浏览器直接访问下面链接），再传到板子：
       ${RAW_BASE}/${TARBALL}
       ${SILERO_URL}
     传输方法见《6.SSH连接与文件传输.md》§5.4（tar 管道）或 §5.6（NFS）。

  2) 换一个加速源重试：
       bash download_asr_model.sh --via ghfast
       bash download_asr_model.sh --via gh-proxy
       bash download_asr_model.sh --via ghproxy

  3) 让板子走有线接路由器上网后重试（见《6.SSH连接与文件传输.md》§3.2）。
EOF
    exit 1
fi

# ── 3. 解压 ─────────────────────────────────────────────────────────────
say "2/5 解压"
cd "$TMP" || exit 1
EXTRACTED=0
if tar -xjf "$TARBALL" 2>/dev/null; then
    EXTRACTED=1
elif tar -xf "$TARBALL" 2>/dev/null; then
    EXTRACTED=1
fi

if [ "$EXTRACTED" != "1" ]; then
    err "解压失败。"
    echo "  Ubuntu 上请先装 bzip2：sudo apt install -y bzip2"
    echo "  Windows 上可以用 7-Zip 手动解压，再把 model.int8.onnx 和 tokens.txt 放进："
    echo "    $DEST"
    exit 1
fi
echo "  解压完成"

SRC_DIR=""
for d in "$TMP"/*/; do
    if [ -f "$d/model.int8.onnx" ] || [ -f "$d/model.onnx" ]; then
        SRC_DIR="$d"
        break
    fi
done
if [ -z "$SRC_DIR" ]; then
    err "解压后没找到模型文件，请检查压缩包是否完整"
    ls -R "$TMP" | head -30
    exit 1
fi
echo "  模型目录：$SRC_DIR"

# ── 4. 安装到目标目录 ───────────────────────────────────────────────────
say "3/5 安装到 $DEST"
FOUND=0
for f in model.int8.onnx model.onnx tokens.txt; do
    if [ -f "$SRC_DIR/$f" ]; then
        cp -f "$SRC_DIR/$f" "$DEST/" && echo "  + $f"
        [ "$f" != "tokens.txt" ] && FOUND=1
    fi
done
if [ -d "$SRC_DIR/test_wavs" ]; then
    cp -rf "$SRC_DIR/test_wavs" "$DEST/" && echo "  + test_wavs/（官方测试音频，用于离线验证）"
fi
[ -f "$SRC_DIR/README.md" ] && cp -f "$SRC_DIR/README.md" "$DEST/" 2>/dev/null

if [ "$FOUND" != "1" ]; then
    err "没有复制到任何模型文件"
    exit 1
fi

say "4/5 下载 Silero VAD（0.6 MB，用于自动断句）"
if [ -f "$DEST/silero_vad.onnx" ] && [ "$FORCE" != "1" ]; then
    echo "  已存在，跳过"
else
    VOK=0
    for src in $SOURCES; do
        PREFIX="$(mirror_prefix "$src")"
        if dl "${PREFIX}${SILERO_URL}" "$DEST/silero_vad.onnx"; then
            SZ=$(wc -c < "$DEST/silero_vad.onnx" 2>/dev/null || echo 0)
            if [ "$SZ" -gt 400000 ]; then VOK=1; echo "  成功（$((SZ / 1024)) KB）"; break; fi
        fi
    done
    if [ "$VOK" != "1" ]; then
        rm -f "$DEST/silero_vad.onnx"
        warn "Silero VAD 下载失败。不影响使用——自动聆听模式会自动退回能量断句。"
    fi
fi

# ── 5. 校验 ─────────────────────────────────────────────────────────────
say "5/5 校验结果"
ls -lh "$DEST" | sed 's/^/  /'
echo
"$CAR_PY" - "$DEST" <<'PY' 2>/dev/null || echo "  （板子上没有可用的 python3，跳过详细校验）"
import os, sys
d = sys.argv[1]
need = ["tokens.txt"]
model = None
for c in ("model.int8.onnx", "model.onnx"):
    p = os.path.join(d, c)
    if os.path.isfile(p):
        model = p
        break
ok = True
if model is None:
    print("  [X] 缺少模型文件 (model.int8.onnx)"); ok = False
else:
    print("  [OK] %s  %.1f MB" % (os.path.basename(model), os.path.getsize(model)/1048576))
for n in need:
    p = os.path.join(d, n)
    if os.path.isfile(p):
        print("  [OK] %s  %.0f KB" % (n, os.path.getsize(p)/1024))
    else:
        print("  [X] 缺少 %s" % n); ok = False
vad = os.path.join(d, "silero_vad.onnx")
print("  [%s] silero_vad.onnx" % ("OK" if os.path.isfile(vad) else "--"))
print("\n  结论：%s" % ("模型就绪" if ok else "模型不完整，请重跑本脚本"))
sys.exit(0 if ok else 1)
PY

say "完成"
cat <<EOF
  接下来验证（不需要图形界面，SSH 里就能跑）：
      cd $(dirname "$0")
      bash run_py.sh asr_engine.py --status --model-dir "$DEST"
      bash run_py.sh asr_engine.py --model-dir "$DEST" --file "$DEST/test_wavs/zh.wav"

  期望最后一行输出类似：识别结果：开放时间早上9点至下午5点。

  然后就能语音控车了：
      bash run_py.sh asr_cli.py --model-dir "$DEST" --no-exec     # 只识别，安全
      bash run_py.sh asr_cli.py --model-dir "$DEST" --port /dev/ttyCAR

  想在 PC 上先下好再传到板子？整个目录一起传即可：
      tar czf - -C "$DEST" . | ssh root@<板子IP> "mkdir -p /home/HwHiAiUser/models/sense-voice && tar xzf - -C /home/HwHiAiUser/models/sense-voice"
EOF
