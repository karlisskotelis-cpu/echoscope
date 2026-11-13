"""Render echoscope.ico (radar arc + accent spoke) with cairo. Run before PyInstaller."""
import io
import math
import os
import struct

import cairo

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'echoscope.ico')
SIZES = [16, 24, 32, 48, 64, 128, 256]


def render(size: int) -> bytes:
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    c = cairo.Context(s)
    c.scale(size / 24.0, size / 24.0)
    # rounded dark tile
    r = 5.0
    c.new_sub_path()
    c.arc(24 - r, r, r, -math.pi / 2, 0)
    c.arc(24 - r, 24 - r, r, 0, math.pi / 2)
    c.arc(r, 24 - r, r, math.pi / 2, math.pi)
    c.arc(r, r, r, math.pi, 3 * math.pi / 2)
    c.close_path()
    c.set_source_rgb(0.063, 0.086, 0.118)
    c.fill()
    c.set_line_cap(cairo.LINE_CAP_ROUND)
    # arc
    c.set_source_rgba(0.906, 0.929, 0.957, 0.9)
    c.set_line_width(2.0)
    c.new_sub_path()
    c.arc(12, 18, 9, math.pi, 2 * math.pi)
    c.stroke()
    # accent spoke
    c.set_source_rgb(0.243, 0.910, 1.0)
    c.set_line_width(2.8)
    c.move_to(12, 18)
    c.line_to(17, 9)
    c.stroke()
    c.set_source_rgba(0.906, 0.929, 0.957, 1)
    c.arc(12, 18, 1.8, 0, 2 * math.pi)
    c.fill()
    buf = io.BytesIO()
    s.write_to_png(buf)
    return buf.getvalue()


def main() -> None:
    pngs = [(sz, render(sz)) for sz in SIZES]
    header = struct.pack('<HHH', 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries = b''
    data = b''
    for sz, png in pngs:
        entries += struct.pack('<BBBBHHII', sz if sz < 256 else 0, sz if sz < 256 else 0, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    with open(OUT, 'wb') as f:
        f.write(header + entries + data)
    print('wrote', OUT)


if __name__ == '__main__':
    main()
