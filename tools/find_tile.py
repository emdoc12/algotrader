"""Locate the icon tile inside a presentation render, and emit the final PNG.

A supplied app icon is usually a *mockup*: the rounded-square tile floating on a
backdrop, with margin, a drop shadow and often a vignette. Uploading that whole
frame as an apple-touch-icon leaves iOS masking an already-inset image, so the
home-screen icon looks small and ringed by dead space.

Thresholding the frame's brightness does not find the tile — the backdrop
gradient and the shadow cross any global threshold, which is how a first attempt
produced a crop containing the vignette instead of the artwork. This instead
looks for the tile's EDGES: the strongest sustained gradient running down a
column and across a row, which is the tile's border against the backdrop.

    python tools/find_tile.py rgb.npy out.png [--size 512]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np


def find_tile(rgb: np.ndarray) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of the tile, falling back to the full frame."""
    lum = rgb.astype(np.float64).mean(axis=2)
    h, w = lum.shape

    # Column and row profiles of how much each line differs from its neighbours.
    # The tile border shows up as a pair of tall spikes on each axis.
    col = np.abs(np.diff(lum.mean(axis=0)))
    row = np.abs(np.diff(lum.mean(axis=1)))

    def edges(profile: float, lo_frac: float, hi_frac: float) -> tuple[int, int]:
        n = len(profile)
        lo_lim, hi_lim = int(n * lo_frac), int(n * hi_frac)
        first = int(np.argmax(profile[:lo_lim])) if lo_lim > 1 else 0
        last = hi_lim + int(np.argmax(profile[hi_lim:])) if hi_lim < n - 1 else n - 1
        return first, last

    x0, x1 = edges(col, 0.45, 0.55)
    y0, y1 = edges(row, 0.45, 0.55)

    # Sanity: the tile should be a big, roughly square region. If the detection
    # is implausible, fall back to the largest centred square — a slightly loose
    # crop is far better than a confidently wrong one.
    tw, th = x1 - x0, y1 - y0
    if tw < w * 0.4 or th < h * 0.3 or not (0.7 < (tw / max(th, 1)) < 1.4):
        s = min(w, h)
        return (w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s
    return x0, y0, x1, y1


def square(box: tuple[int, int, int, int], w: int, h: int) -> tuple[int, int, int, int]:
    """Expand/shrink a box to a square, kept inside the frame."""
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    s = min(max(x1 - x0, y1 - y0), w, h)
    nx0 = int(round(max(0, min(cx - s / 2, w - s))))
    ny0 = int(round(max(0, min(cy - s / 2, h - s))))
    return nx0, ny0, nx0 + int(s), ny0 + int(s)


def resize_area(rgb: np.ndarray, size: int) -> np.ndarray:
    h, w, _ = rgb.shape
    ys = (np.arange(size + 1) * h / size).astype(int)
    xs = (np.arange(size + 1) * w / size).astype(int)
    src = rgb.astype(np.float64)
    out = np.zeros((size, size, 3))
    for i in range(size):
        band = src[ys[i]:max(ys[i] + 1, ys[i + 1])]
        for j in range(size):
            out[i, j] = band[:, xs[j]:max(xs[j] + 1, xs[j + 1])].mean(axis=(0, 1))
    return np.clip(out, 0, 255).astype(np.uint8)


def preview(rgb: np.ndarray, n: int = 34) -> str:
    lum = rgb.astype(float).mean(2)
    H, W = lum.shape
    small = lum[:n * (H // n), :n * (W // n)].reshape(n, H // n, n, W // n).mean((1, 3))
    chars = " .:-=+*#%@"
    return "\n".join("  " + "".join(chars[min(int(v / 256 * len(chars)), len(chars) - 1)]
                                    for v in r) for r in small)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    size = 512
    for a in sys.argv[1:]:
        if a.startswith("--size"):
            size = int(a.split("=")[1])
    rgb = np.load(args[0])
    h, w, _ = rgb.shape
    box = square(find_tile(rgb), w, h)
    print(f"frame {w}x{h} -> tile {box}")
    crop = rgb[box[1]:box[3], box[0]:box[2]]
    print(preview(crop))
    out = resize_area(crop, size)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from make_icon import write_png
    dst = Path(args[1])
    write_png(dst, size, size, out.tobytes())
    print(f"wrote {dst} ({dst.stat().st_size:,} bytes, {size}x{size})")


if __name__ == "__main__":
    main()
