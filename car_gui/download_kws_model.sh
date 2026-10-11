#!/usr/bin/env bash
# ============================================================================
#  download_kws_model.sh —— 下载 KWS 关键词识别模型（约 32 MB）
#
#  产出目录（默认 ~/models/kws/）：
#      sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/
#          encoder-epoch-99-avg-1-chunk-16-left-64.onnx   12.1 MB
#          decoder-epoch-99-avg-1-chunk-16-left-64.onnx    0.7 MB
#          joiner-epoch-99-avg-1-chunk-16-left-64.onnx     0.25 MB
#          tokens.txt                                       1.6 KB
#          test_wavs/*.wav                                  官方测试音频
#
#  用法：
#      bash download_kws_model.sh                    # 自动选源，装到 ~/models/kws
#      bash download_kws_model.sh --dest /data/models/kws
#      bash download_kws_model.sh --via ghfast       # 强制用某个加速源
#      bash download_kws_model.sh --force            # 已存在也重新下载
#
#  ⚠️ 不要用模型包里的 *.int8.onnx 版本！
#     本板 sherpa_onnx 1.13.8 加载 int8 会报
#     `'attention_dims' does not exist in the metadata`。
#     kws_engine.py 已自动避开 int8。
#
#  ⚠️ 本脚本下载的是**模型**，不含关键词表。
#     急停关键词表在同目录的 kws_keywords_emergency.txt。
# ============================================================================
set -u

# 统一解析解释器（本板多套 Python，依赖只装在其中一套里）
# shellcheck source=_pyenv.sh
. "$(dirname "$0")/_pyenv.sh"

DEST="$HOME/models/kws"
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

MODEL_NAME="sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
TARBALL="${MODEL_NAME}.tar.bz2"
RAW_BASE="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models"
MODEL_DIR="$DEST/$MODEL_NAME"

# 三个核心文件齐全才算装好
need_download() {
    [ "$FORCE" = "1" ] && return 0
    [ -f "$MODEL_DIR/encoder-epoch-99-avg-1-chunk-16-left-64.onnx" ] && \
    [ -f "$MODEL_DIR/decoder-epoch-99-avg-1-chunk-16-left-64.onnx" ] && \
    [ -f "$MODEL_DIR/joiner-epoch-99-avg-1-chunk-16-left-64.onnx" ] && \
    [ -f "$MODEL_DIR/tokens.txt" ] && return 1
    return 0
}

# ── 0. 前置检查 ─────────────────────────────────────────────────────────
say "0/4 环境检查"
command -v curl >/dev/null 2>&1 || { err "缺少 curl"; exit 1; }
command -v tar  >/dev/null 2>&1 || { err "缺少 tar"; exit 1; }
echo "  curl   : $(curl --version | head -1)"
echo "  目标目录: $DEST"
mkdir -p "$DEST" || { err "无法创建目录 $DEST"; exit 1; }

if ! need_download; then
    echo
    echo "  模型已存在，跳过下载。"
    echo "  如需重新下载，加 --force 参数。"
    ls -lh "$MODEL_DIR" | sed 's/^/    /'
    exit 0
fi

AVAIL_KB=$(df -Pk "$DEST" | tail -1 | awk '{print $4}')
AVAIL_MB=$((AVAIL_KB / 1024))
echo "  可用空间: ${AVAIL_MB} MB"
if [ "$AVAIL_MB" -lt 200 ]; then
    err "空间不足（需要约 200MB，当前 ${AVAIL_MB}MB）。"
    exit 1
fi

# ── 1. 准备临时目录 ─────────────────────────────────────────────────────
TMP="$(mktemp -d 2>/dev/null || echo /tmp/kws_dl_$$)"
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
    SOURCES="ghfast gh-proxy ghproxy github"
else
    SOURCES="$VIA"
fi

dl() {
    local url="$1" out="$2"
    echo "  下载：$url"
    curl -L --fail --retry 3 --retry-delay 2 --connect-timeout 20 \
         -C - -o "$out" "$url"
}

# ── 3. 下载 ─────────────────────────────────────────────────────────────
say "1/4 下载 KWS 模型压缩包（约 32 MB）"
OK=0
for src in $SOURCES; do
    PREFIX="$(mirror_prefix "$src")"
    URL="${PREFIX}${RAW_BASE}/${TARBALL}"
    echo
    echo "  -- 尝试源：$src"
    if dl "$URL" "$TMP/$TARBALL"; then
        SIZE=$(wc -c < "$TMP/$TARBALL" 2>/dev/null || echo 0)
        # 正常约 32MB；小于 20MB 说明下到的不是模型
        if [ "$SIZE" -gt 20000000 ]; then
            OK=1
            echo "  成功，大小 $((SIZE / 1048576)) MB"
            break
        else
            warn "文件只有 $((SIZE / 1024)) KB，可能不是模型包，换下一个源"
        fi
    else
        warn "该源失败，换下一个"
    fi
done

if [ "$OK" != "1" ]; then
    err "所有下载源都失败了。"
    cat <<EOF

  可选做法：
  1) 在 PC 上下载（浏览器直接访问），再传到板子：
       ${RAW_BASE}/${TARBALL}
     传输方法见《6.SSH连接与文件传输.md》§5.4（tar 管道）或 §5.6（NFS）。

  2) 换一个加速源重试：
       bash download_kws_model.sh --via ghfast

EOF
    exit 1
fi

# ── 4. 解压 ─────────────────────────────────────────────────────────────
say "2/4 解压"
if ! tar xjf "$TMP/$TARBALL" -C "$DEST"; then
    err "解压失败（压缩包可能损坏），加 --force 重新下载"
    exit 1
fi
echo "  解压到 $DEST"

# ── 5. 校验 ─────────────────────────────────────────────────────────────
say "3/4 校验文件"
MISSING=0
for f in encoder-epoch-99-avg-1-chunk-16-left-64.onnx \
         decoder-epoch-99-avg-1-chunk-16-left-64.onnx \
         joiner-epoch-99-avg-1-chunk-16-left-64.onnx \
         tokens.txt; do
    if [ -f "$MODEL_DIR/$f" ]; then
        printf "  ✅ %-48s %s\n" "$f" "$(du -h "$MODEL_DIR/$f" | cut -f1)"
    else
        printf "  ❌ %-48s 缺失\n" "$f"
        MISSING=1
    fi
done
[ -d "$MODEL_DIR/test_wavs" ] && \
    printf "  ✅ %-48s %s 个 wav\n" "test_wavs/" \
           "$(ls "$MODEL_DIR/test_wavs"/*.wav 2>/dev/null | wc -l)"

if [ "$MISSING" = "1" ]; then
    err "文件不全，请加 --force 重试"
    exit 1
fi

# ── 6. 定权限 ───────────────────────────────────────────────────────────
say "4/4 设置权限"
# 模型目录通常被解压成 uid 1001，统一改成当前用户
if [ "$(id -u)" = "0" ] && id HwHiAiUser >/dev/null 2>&1; then
    chown -R HwHiAiUser:HwHiAiUser "$DEST" 2>/dev/null || true
    echo "  已把 $DEST 归属改为 HwHiAiUser"
fi

echo
echo "${C_OK}===== 完成 =====${C_OFF}"
echo "  模型目录：$MODEL_DIR"
echo
echo "  下一步（注意要先跑到 HwHiAiUser 下）："
echo "      sudo su - HwHiAiUser"
echo "      cd ~/car_gui"
echo "      bash run_py.sh kws_engine.py --test-wavs   # 离线自检"
echo "      bash run_py.sh kws_emergency.py --beep     # 实时监听（说\"急停\"）"
echo
echo "  ⚠️ 首次使用前先跑 bash setup_mic_gain.sh（PCM2902 默认增益 0%）"
echo "  ⚠️ 自定义关键词必须用**真人语音**验证，合成音不可用，详见"
echo "     《12.KWS语音急停实施文档.md》§6"
