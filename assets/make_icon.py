"""Draw the menu bar icon. No dependencies — the PNG is written by hand.

Run: python assets/make_icon.py
Output: assets/icon.png — a black glyph with alpha. macOS template mode recolours
it for the light and dark menu bar on its own.
"""
import math
import struct
import sys
import zlib
from pathlib import Path

SIZE = 40        # rumps draws the icon at 20pt; 40px keeps it crisp on retina
SS = 4           # supersampling, for antialiased edges
STROKE = 2.6


def sd_round_rect(px, py, cx, cy, hw, hh, r):
    qx, qy = abs(px - cx) - (hw - r), abs(py - cy) - (hh - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def sd_segment(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    ll = vx * vx + vy * vy
    t = 0.0 if ll == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / ll))
    return math.hypot(wx - t * vx, wy - t * vy)


def glyph(x, y):
    # page outline
    if abs(sd_round_rect(x, y, 20, 20, 12, 15, 3.5)) <= STROKE / 2:
        return True
    # lines of text; the last one is shorter, like the end of a paragraph
    for y0, x1 in ((13.5, 27.0), (20.0, 27.0), (26.5, 23.0)):
        if sd_segment(x, y, 13.0, y0, x1, y0) <= STROKE / 2:
            return True
    return False


def slash(x, y):
    """Diagonal stroke for the paused state, with a gap around it so the
    slash stays readable against the glyph underneath."""
    d = sd_segment(x, y, 8.5, 31.5, 31.5, 8.5)
    return d <= STROKE / 2


def render(inside):
    rows = []
    for py in range(SIZE):
        row = bytearray()
        for px in range(SIZE):
            hits = sum(inside(px + (sx + 0.5) / SS, py + (sy + 0.5) / SS)
                       for sy in range(SS) for sx in range(SS))
            row += bytes((0, 0, 0, round(255 * hits / (SS * SS))))
        rows.append(bytes(row))
    return rows


def paused(x, y):
    # punch a gap out of the glyph so the slash reads as a separate stroke
    if slash(x, y):
        return True
    if sd_segment(x, y, 8.5, 31.5, 31.5, 8.5) <= STROKE * 1.35:
        return False
    return glyph(x, y)


def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path, rows):
    h = len(rows)
    w = len(rows[0]) // 4          # RGBA
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + r for r in rows), 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)
    return len(png)


here = Path(__file__).parent
for name, fn in (("icon.png", glyph), ("icon-paused.png", paused)):
    rows = render(fn)
    size = write_png(here / name, rows)
    print(f"{name} — {size} bytes, {SIZE}x{SIZE}")
    for py in range(SIZE):
        print("  " + "".join(" .:-=+*#%@"[min(9, rows[py][px * 4 + 3] // 26)]
                             for px in range(SIZE)))
    print()


# ---------------------------------------------------------------- app icon

BRAND = (0.13, 0.42, 0.85)      # the rounded square behind the glyph


def glyph_scaled(x, y, size):
    """The same page drawn in a 40-unit space, scaled into `size` and inset."""
    inset = size * 0.235
    span = size - 2 * inset
    if span <= 0:
        return False
    return glyph((x - inset) * 40 / span, (y - inset) * 40 / span)


def app_sample(x, y, size):
    """One supersample of the app icon, as premultiplied RGBA floats."""
    r = size * 0.2237                                   # macOS squircle radius
    if sd_round_rect(x, y, size / 2, size / 2, size * 0.455, size * 0.455, r) > 0:
        return (0.0, 0.0, 0.0, 0.0)
    if glyph_scaled(x, y, size):
        return (1.0, 1.0, 1.0, 1.0)
    return (*BRAND, 1.0)


def render_app(size):
    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(SS):
                for sx in range(SS):
                    s = app_sample(px + (sx + 0.5) / SS, py + (sy + 0.5) / SS, size)
                    for k in range(4):
                        acc[k] += s[k]
            n = SS * SS
            a = acc[3] / n
            if a <= 0:
                row += bytes((0, 0, 0, 0))
            else:   # un-premultiply so the PNG stores straight alpha
                row += bytes(tuple(round(255 * min(1.0, acc[k] / n / a)) for k in range(3))
                             + (round(255 * a),))
        rows.append(bytes(row))
    return rows


def build_icns():
    import shutil
    import subprocess

    iconset = here / "AppIcon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for base in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            px = base * scale
            write_png(iconset / f"icon_{base}x{base}{suffix}.png", render_app(px))
    out = here / "AppIcon.icns"
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    shutil.rmtree(iconset, ignore_errors=True)
    print(f"AppIcon.icns — {out.stat().st_size} bytes")


if "--icns" in sys.argv:
    build_icns()
