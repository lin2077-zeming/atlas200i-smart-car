#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xwd2png.py —— 把 XWD 截图转成 PNG（**纯标准库，不需要 numpy / Pillow**）

为什么需要它：
    本板（Atlas 200I DK A2，Ubuntu 22.04）只有 `xwd`，**没有** convert / magick /
    xwdtopnm，Pillow 也不认 xwd 格式；PC 侧的 python 也可能没有 numpy/PIL。
    → 用标准库 struct + zlib 手工解析并写 PNG，两边都能跑。

用法：
    python3 xwd2png.py shot.xwd shot.png
    python3 xwd2png.py shot.xwd            # 默认同名 .png
    python3 xwd2png.py --info shot.xwd     # 只打印头部信息

XWD 头（大端 u32；header_size 给出真实长度）：
   0 header_size   4 file_version  8 pixmap_format 12 pixmap_depth
  16 pixmap_width 20 pixmap_height 24 xoffset 28 byte_order
  32 bitmap_unit  36 bitmap_bit_order 40 bitmap_pad 44 bits_per_pixel
  48 bytes_per_line 52 visual_class 56 red_mask 60 green_mask 64 blue_mask
  68 bits_per_rgb 72 colormap_entries 76 ncolors 80 window_width
  84 window_height 88 window_x 92 window_y 96 window_bdrwidth
像素起始 = header_size + ncolors * 12（每项 XWDColor 是 12 字节）
"""
import struct
import sys
import zlib


def read_header(d):
    f = struct.unpack(">25I", d[:100])
    h = {
        "header_size": f[0], "file_version": f[1], "pixmap_format": f[2],
        "depth": f[3], "width": f[4], "height": f[5], "xoffset": f[6],
        "byte_order": f[7], "bitmap_unit": f[8], "bitmap_bit_order": f[9],
        "bitmap_pad": f[10], "bpp": f[11], "bytes_per_line": f[12],
        "visual_class": f[13], "red_mask": f[14], "green_mask": f[15],
        "blue_mask": f[16], "bits_per_rgb": f[17], "colormap_entries": f[18],
        "ncolors": f[19], "window_width": f[20], "window_height": f[21],
        "window_x": f[22], "window_y": f[23], "window_bdrwidth": f[24],
    }
    return h


def _shift(mask):
    """返回 (右移位数, 位宽) —— 用于把像素值抽成 0~255。"""
    if mask == 0:
        return 0, 0
    s = 0
    m = mask
    while (m & 1) == 0:
        m >>= 1
        s += 1
    return s, bin(m).count("1")


def to_rgb_rows(d, h):
    """把 XWD 像素解成每行 RGB 字节串（每像素 3 字节）。"""
    w, ht = h["width"], h["height"]
    bpl = h["bytes_per_line"]
    bpp = h["bpp"]
    off = h["header_size"] + h["ncolors"] * 12
    px_bytes = (bpp + 7) // 8
    rs, rb = _shift(h["red_mask"])
    gs, gb = _shift(h["green_mask"])
    bs, bb = _shift(h["blue_mask"])
    le = (h["byte_order"] == 0)      # 0 = LSBFirst

    def scale(v, bits):
        if bits <= 0:
            return 0
        mx = (1 << bits) - 1
        return (v * 255 + mx // 2) // mx

    rows = []
    for y in range(ht):
        base = off + y * bpl
        line = d[base:base + bpl]
        out = bytearray(w * 3)
        for x in range(w):
            p = line[x * px_bytes:(x + 1) * px_bytes]
            v = int.from_bytes(p, "little" if le else "big")
            r = scale((v & h["red_mask"]) >> rs, rb)
            g = scale((v & h["green_mask"]) >> gs, gb)
            b = scale((v & h["blue_mask"]) >> bs, bb)
            o = x * 3
            out[o] = r
            out[o + 1] = g
            out[o + 2] = b
        rows.append(bytes(out))
    return rows


def write_png(path, rows, width, height):
    raw = b"".join(b"\x00" + r for r in rows)      # 每行前面加 filter 类型 0

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)
    return len(png)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    info_only = "--info" in sys.argv
    if not args:
        print(__doc__)
        return 2
    src = args[0]
    dst = args[1] if len(args) > 1 else (src.rsplit(".", 1)[0] + ".png")

    with open(src, "rb") as f:
        d = f.read()
    h = read_header(d)
    print("XWD: %dx%d depth=%d bpp=%d bpl=%d ncolors=%d header=%d" % (
        h["width"], h["height"], h["depth"], h["bpp"],
        h["bytes_per_line"], h["ncolors"], h["header_size"]))
    print("     visual_class=%d R=%08x G=%08x B=%08x byte_order=%d" % (
        h["visual_class"], h["red_mask"], h["green_mask"],
        h["blue_mask"], h["byte_order"]))
    if info_only:
        return 0

    rows = to_rgb_rows(d, h)
    n = write_png(dst, rows, h["width"], h["height"])
    print("已写出 %s（%d 字节）" % (dst, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
