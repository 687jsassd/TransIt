#!/usr/bin/env python3
"""生成 TransIt 应用图标（纯标准库，无 Pillow 依赖）。

产出：
  assets/transit.ico  多尺寸 ICO（16/24/32/48/64/128/256），供 PyInstaller 使用
  assets/transit.png  256x256 PNG 预览

设计：WebUI 主题色渐变（#7c5cff → #38bdf8）圆角方块 + 白色右向箭头（"翻译/转换"）。
所有形状用超采样抗锯齿绘制，缩到 16px 仍然清晰可辨。

用法：python tools/make_icon.py
"""
import os
import struct
import zlib

# 与 web/index.html 的 --accent / --accent2 保持一致
C_FROM = (0x7C, 0x5C, 0xFF)
C_TO = (0x38, 0xBD, 0xF8)
SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4  # 超采样倍数


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _in_rounded_rect(x, y, w, radius):
    """点是否落在边距 margin 的圆角正方形内（坐标已归一化到 [0,w]）。"""
    r = radius * w
    cx = min(max(x, r), w - r)
    cy = min(max(y, r), w - r)
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _arrow_polygon(w):
    """右向箭头多边形（归一化坐标乘以边长 w）。"""
    pts = [(0.200, 0.435), (0.495, 0.435), (0.495, 0.295),
           (0.805, 0.500), (0.495, 0.705), (0.495, 0.565),
           (0.200, 0.565)]
    return [(px * w, py * w) for px, py in pts]


def _in_polygon(x, y, poly):
    """射线法判断点是否在多边形内。"""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            xin = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < xin:
                inside = not inside
        j = i
    return inside


def render_rgba(size):
    """返回 size x size 的 RGBA bytes（超采样抗锯齿）。"""
    w = float(size)
    radius = 0.22
    poly = _arrow_polygon(w)
    step = 1.0 / SS
    off = step / 2.0
    out = bytearray()

    for py in range(size):
        for px in range(size):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in range(SS):
                y = py + off + sy * step
                for sx in range(SS):
                    x = px + off + sx * step
                    if not _in_rounded_rect(x, y, w, radius):
                        continue
                    if _in_polygon(x, y, poly):
                        r, g, b = 255, 255, 255
                    else:
                        # 45° 线性渐变
                        t = min(1.0, max(0.0, (x + y) / (2.0 * w)))
                        r, g, b = _lerp(C_FROM, C_TO, t)
                    # 预乘 alpha 参与平均，避免边缘出现深色描边
                    acc_r += r
                    acc_g += g
                    acc_b += b
                    acc_a += 255.0
            n = float(SS * SS)
            a = acc_a / n
            if a <= 0:
                out += bytes((0, 0, 0, 0))
            else:
                # 反预乘：颜色取"有覆盖的子像素"均值
                cover = acc_a / 255.0
                out += bytes((
                    int(round(acc_r / cover)),
                    int(round(acc_g / cover)),
                    int(round(acc_b / cover)),
                    int(round(a)),
                ))
    return bytes(out)


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(size, rgba):
    """把 RGBA 像素编码为 PNG。"""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type: None
        raw += rgba[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def encode_ico(images):
    """images: [(size, png_bytes)] -> ICO 字节（内嵌 PNG，Vista+ 支持）。"""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = b""
    payload = b""
    offset = 6 + 16 * count
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(png), offset + len(payload))
        payload += png
    return header + entries + payload


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "assets")
    os.makedirs(out_dir, exist_ok=True)

    images = []
    for size in SIZES:
        png = encode_png(size, render_rgba(size))
        images.append((size, png))
        print(f"  rendered {size}x{size}")

    ico_path = os.path.join(out_dir, "transit.ico")
    with open(ico_path, "wb") as f:
        f.write(encode_ico(images))

    png_path = os.path.join(out_dir, "transit.png")
    with open(png_path, "wb") as f:
        f.write(images[-1][1])

    print(f"wrote {ico_path} ({os.path.getsize(ico_path):,} bytes)")
    print(f"wrote {png_path} ({os.path.getsize(png_path):,} bytes)")


if __name__ == "__main__":
    main()
