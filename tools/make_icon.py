"""Generate the app icon as a PNG, with no third-party dependencies.

Pillow is not installed in this project and adding it once cost a debugging
session (the install clobbered pandas), so the icon is drawn by hand and encoded
with nothing but ``zlib`` and ``struct`` from the standard library. Run it to
regenerate the icon after changing the design:

    python tools/make_icon.py

Design notes:

  * **No rounded corners.** iOS applies its own squircle mask to an
    apple-touch-icon; rounding it here would show as a dark halo inside Apple's
    mask, which is the classic way a home-screen icon ends up looking wrong.
  * **Rendered at 4x and box-downsampled**, which is how the diagonal chart line
    gets clean anti-aliased edges without a graphics library.
  * Colours are the dashboard's own tokens, so the icon and the page it opens
    look like the same product.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 180
SS = 4                      # supersampling factor
W = H = SIZE * SS

# Dashboard palette (see PAGE_HTML :root)
BG_TOP = (0x16, 0x16, 0x1B)
BG_BOT = (0x0A, 0x0A, 0x0C)
GREEN = (0x36, 0xD3, 0x99)
GREEN_DIM = (0x36, 0xD3, 0x99)
ACCENT = (0x7C, 0x8C, 0xFF)


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _blend(dst, src, alpha):
    return tuple(int(round(dst[i] + (src[i] - dst[i]) * alpha)) for i in range(3))


def _dist_to_segment(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx, cy = x1 + t * dx, y1 + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def render() -> list[list[tuple]]:
    # vertical gradient background
    px = [[_lerp(BG_TOP, BG_BOT, y / (H - 1)) for _ in range(W)] for y in range(H)]

    # A rising P&L line — the thing this app is actually about. Points are in
    # 0..1 space so the shape is independent of the raster size.
    pts01 = [(0.14, 0.70), (0.34, 0.52), (0.50, 0.62), (0.68, 0.34), (0.86, 0.24)]
    pts = [(x * W, y * H) for x, y in pts01]
    half = 0.052 * W                     # line half-width
    glow = half * 2.6

    # Soft glow under the line, then the line itself. Doing the glow first means
    # the crisp stroke stays crisp on top of it.
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        lo_x = max(0, int(min(x1, x2) - glow)); hi_x = min(W, int(max(x1, x2) + glow) + 1)
        lo_y = max(0, int(min(y1, y2) - glow)); hi_y = min(H, int(max(y1, y2) + glow) + 1)
        for y in range(lo_y, hi_y):
            row = px[y]
            for x in range(lo_x, hi_x):
                d = _dist_to_segment(x + 0.5, y + 0.5, x1, y1, x2, y2)
                if d < glow:
                    a = (1.0 - d / glow) ** 2 * 0.22
                    row[x] = _blend(row[x], GREEN_DIM, a)

    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        lo_x = max(0, int(min(x1, x2) - half) - 2); hi_x = min(W, int(max(x1, x2) + half) + 3)
        lo_y = max(0, int(min(y1, y2) - half) - 2); hi_y = min(H, int(max(y1, y2) + half) + 3)
        for y in range(lo_y, hi_y):
            row = px[y]
            for x in range(lo_x, hi_x):
                d = _dist_to_segment(x + 0.5, y + 0.5, x1, y1, x2, y2)
                if d <= half:
                    row[x] = GREEN

    # Terminal dot at the apex, ringed in the background colour so it reads as a
    # distinct marker rather than a blob on the end of the stroke.
    cx, cy = pts[-1]
    r_out, r_in = half * 2.15, half * 1.45
    lo_x = max(0, int(cx - r_out) - 2); hi_x = min(W, int(cx + r_out) + 3)
    lo_y = max(0, int(cy - r_out) - 2); hi_y = min(H, int(cy + r_out) + 3)
    for y in range(lo_y, hi_y):
        row = px[y]
        for x in range(lo_x, hi_x):
            d = ((x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2) ** 0.5
            if d <= r_in:
                row[x] = GREEN
            elif d <= r_out:
                row[x] = _lerp(BG_TOP, BG_BOT, y / (H - 1))
    return px


def downsample(px) -> bytes:
    """Box-filter SS x SS blocks down to the final size — this is the anti-aliasing."""
    out = bytearray()
    n = SS * SS
    for oy in range(SIZE):
        base = oy * SS
        for ox in range(SIZE):
            bx = ox * SS
            r = g = b = 0
            for dy in range(SS):
                row = px[base + dy]
                for dx in range(SS):
                    p = row[bx + dx]
                    r += p[0]; g += p[1]; b += p[2]
            out += bytes((r // n, g // n, b // n))
    return bytes(out)


def _filter_scanlines(width: int, height: int, rgb: bytes) -> bytes:
    """Adaptive per-row PNG filtering (the standard minimum-sum-of-absolute-
    differences heuristic).

    Writing every row unfiltered is valid PNG but compresses badly on
    photographic content — the icon came out at 416KB unfiltered and 
    roughly a third of that once each row picks its best filter.
    """
    bpp = 3
    out = bytearray()
    prev = bytearray(width * bpp)
    for y in range(height):
        line = rgb[y * width * bpp:(y + 1) * width * bpp]
        cands = []
        # 0: None
        cands.append((0, bytes(line)))
        # 1: Sub
        sub = bytearray(len(line))
        for i in range(len(line)):
            left = line[i - bpp] if i >= bpp else 0
            sub[i] = (line[i] - left) & 0xFF
        cands.append((1, bytes(sub)))
        # 2: Up
        up = bytearray(len(line))
        for i in range(len(line)):
            up[i] = (line[i] - prev[i]) & 0xFF
        cands.append((2, bytes(up)))
        # 3: Average
        avg = bytearray(len(line))
        for i in range(len(line)):
            left = line[i - bpp] if i >= bpp else 0
            avg[i] = (line[i] - ((left + prev[i]) >> 1)) & 0xFF
        cands.append((3, bytes(avg)))
        # 4: Paeth
        pae = bytearray(len(line))
        for i in range(len(line)):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            pp = a + b - c
            pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            pae[i] = (line[i] - pr) & 0xFF
        cands.append((4, bytes(pae)))
        ftype, data = min(cands, key=lambda t: sum(v if v < 128 else 256 - v for v in t[1]))
        out += bytes([ftype]) + data
        prev = bytearray(line)
    return bytes(out)


def write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    raw = _filter_scanlines(width, height, rgb)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> None:
    dest = Path(__file__).resolve().parents[1] / "daytrader" / "live" / "static" / "apple-touch-icon.png"
    write_png(dest, SIZE, SIZE, downsample(render()))
    print(f"wrote {dest} ({dest.stat().st_size:,} bytes, {SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
