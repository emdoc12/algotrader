"""Decode a baseline JPEG and write a square PNG app icon — no image libraries.

Pillow is deliberately not installed in this project (adding it once clobbered
pandas), and the container has no ImageMagick, djpeg or ffmpeg either. The icon
the owner supplied is a JPEG, and Safari renders ONLY a PNG as an
apple-touch-icon — renaming the file does not convert it. So this decodes
baseline JPEG directly: Huffman → dequantise → IDCT → chroma upsample →
YCbCr→RGB, with numpy doing the IDCT in one batched einsum.

It then finds the icon artwork inside the image. A supplied icon is usually a
presentation render — the tile floating on a background with margin — and
uploading that as-is leaves iOS masking an already-inset icon, so it looks small
and ringed. The tile is located by brightness against the background rather than
assumed to be centred.

    python tools/jpeg_to_icon.py INPUT.jpg [OUTPUT.png] [--size 512] [--no-autocrop]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ZIGZAG = np.array([
    0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63])


class JpegError(ValueError):
    pass


class _BitReader:
    """Entropy-coded bit stream, handling 0xFF00 byte stuffing and restarts."""

    def __init__(self, data: bytes, pos: int):
        self.d = data
        self.i = pos
        self.bits = 0
        self.nbits = 0

    def _fill(self) -> None:
        while self.nbits <= 24:
            if self.i >= len(self.d):
                self.bits = (self.bits << 8) | 0
                self.nbits += 8
                continue
            b = self.d[self.i]
            if b == 0xFF:
                nxt = self.d[self.i + 1] if self.i + 1 < len(self.d) else 0
                if nxt == 0x00:
                    self.i += 2
                elif 0xD0 <= nxt <= 0xD7:      # restart marker: stop here
                    self.bits = (self.bits << 8) | 0
                    self.nbits += 8
                    continue
                else:                          # a real marker ends the scan
                    self.bits = (self.bits << 8) | 0
                    self.nbits += 8
                    continue
            else:
                self.i += 1
            self.bits = (self.bits << 8) | b
            self.nbits += 8

    def bit(self) -> int:
        if self.nbits == 0:
            self._fill()
        self.nbits -= 1
        return (self.bits >> self.nbits) & 1

    def receive(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def align_restart(self) -> None:
        """Skip to just past the next RSTn marker."""
        self.bits = 0
        self.nbits = 0
        while self.i < len(self.d) - 1:
            if self.d[self.i] == 0xFF and 0xD0 <= self.d[self.i + 1] <= 0xD7:
                self.i += 2
                return
            self.i += 1


def _extend(v: int, t: int) -> int:
    return v - (1 << t) + 1 if t and v < (1 << (t - 1)) else v


def _build_huff(bits: list[int], vals: list[int]) -> dict:
    """(length, code) -> value lookup."""
    table, code, k = {}, 0, 0
    for ln in range(1, 17):
        for _ in range(bits[ln - 1]):
            table[(ln, code)] = vals[k]
            k += 1
            code += 1
        code <<= 1
    return table


def _decode_huff(br: _BitReader, table: dict) -> int:
    code, ln = 0, 0
    for _ in range(16):
        code = (code << 1) | br.bit()
        ln += 1
        v = table.get((ln, code))
        if v is not None:
            return v
    raise JpegError("bad Huffman code")


def _idct_matrix() -> np.ndarray:
    x = np.arange(8)
    u = np.arange(8)
    c = np.where(u == 0, 1 / np.sqrt(2), 1.0)
    return (c[None, :] * np.cos((2 * x[:, None] + 1) * u[None, :] * np.pi / 16)) / 2.0


def decode_jpeg(data: bytes) -> np.ndarray:
    """Baseline JPEG bytes -> HxWx3 uint8 RGB."""
    if data[:2] != b"\xff\xd8":
        raise JpegError("not a JPEG")
    qt: dict[int, np.ndarray] = {}
    huff_dc: dict[int, dict] = {}
    huff_ac: dict[int, dict] = {}
    frame = None
    restart_interval = 0
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        i += 2
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            continue
        if m == 0xD9:
            break
        seg_len = int.from_bytes(data[i:i + 2], "big")
        seg = data[i + 2:i + seg_len]
        if m == 0xDB:                                   # quantisation tables
            p = 0
            while p < len(seg):
                pq, tq = seg[p] >> 4, seg[p] & 15
                p += 1
                n = 64 * (2 if pq else 1)
                raw = seg[p:p + n]
                vals = (np.frombuffer(raw, dtype=">u2") if pq
                        else np.frombuffer(raw, dtype=np.uint8)).astype(np.float64)
                tbl = np.zeros(64)
                tbl[ZIGZAG] = vals
                qt[tq] = tbl.reshape(8, 8)
                p += n
        elif m == 0xC4:                                 # Huffman tables
            p = 0
            while p < len(seg):
                tc, th = seg[p] >> 4, seg[p] & 15
                bits = list(seg[p + 1:p + 17])
                n = sum(bits)
                vals = list(seg[p + 17:p + 17 + n])
                (huff_ac if tc else huff_dc)[th] = _build_huff(bits, vals)
                p += 17 + n
        elif m in (0xC0, 0xC1):                         # baseline frame
            h = int.from_bytes(seg[1:3], "big")
            w = int.from_bytes(seg[3:5], "big")
            nc = seg[5]
            comps = []
            for c in range(nc):
                cid, hv, tq = seg[6 + c * 3], seg[7 + c * 3], seg[8 + c * 3]
                comps.append({"id": cid, "h": hv >> 4, "v": hv & 15, "tq": tq})
            frame = {"w": w, "h": h, "comps": comps}
        elif m == 0xC2:
            raise JpegError("progressive JPEG is not supported by this decoder")
        elif m == 0xDD:
            restart_interval = int.from_bytes(seg[0:2], "big")
        elif m == 0xDA:                                 # start of scan
            ns = seg[0]
            scan = []
            for c in range(ns):
                cs, td_ta = seg[1 + c * 2], seg[2 + c * 2]
                scan.append({"id": cs, "dc": td_ta >> 4, "ac": td_ta & 15})
            return _decode_scan(data, i + seg_len, frame, scan, qt,
                                huff_dc, huff_ac, restart_interval)
        i += seg_len
    raise JpegError("no scan found")


def _decode_scan(data, pos, frame, scan, qt, huff_dc, huff_ac, restart_interval):
    if frame is None:
        raise JpegError("scan before frame header")
    W, H = frame["w"], frame["h"]
    comps = frame["comps"]
    hmax = max(c["h"] for c in comps)
    vmax = max(c["v"] for c in comps)
    mcux = (W + 8 * hmax - 1) // (8 * hmax)
    mcuy = (H + 8 * vmax - 1) // (8 * vmax)

    for c in comps:
        c["bw"] = mcux * c["h"]
        c["bh"] = mcuy * c["v"]
        c["blocks"] = np.zeros((c["bh"] * c["bw"], 8, 8), dtype=np.float64)
        c["n"] = 0
        s = next((x for x in scan if x["id"] == c["id"]), scan[0])
        c["dct"], c["act"] = huff_dc[s["dc"]], huff_ac[s["ac"]]

    br = _BitReader(data, pos)
    pred = {c["id"]: 0 for c in comps}
    mcu_count = 0
    for my in range(mcuy):
        for mx in range(mcux):
            if restart_interval and mcu_count and mcu_count % restart_interval == 0:
                br.align_restart()
                pred = {c["id"]: 0 for c in comps}
            for c in comps:
                for by in range(c["v"]):
                    for bx in range(c["h"]):
                        coef = np.zeros(64)
                        t = _decode_huff(br, c["dct"])
                        diff = _extend(br.receive(t), t) if t else 0
                        pred[c["id"]] += diff
                        coef[0] = pred[c["id"]]
                        k = 1
                        while k < 64:
                            rs = _decode_huff(br, c["act"])
                            r, s = rs >> 4, rs & 15
                            if s == 0:
                                if r == 15:
                                    k += 16
                                    continue
                                break
                            k += r
                            if k > 63:
                                break
                            coef[ZIGZAG[k]] = _extend(br.receive(s), s)
                            k += 1
                        row = my * c["v"] + by
                        col = mx * c["h"] + bx
                        c["blocks"][row * c["bw"] + col] = coef.reshape(8, 8)
            mcu_count += 1

    M = _idct_matrix()
    planes = []
    for c in comps:
        deq = c["blocks"] * qt[c["tq"]][None, :, :]
        px = np.einsum("ij,njk,lk->nil", M, deq, M) + 128.0
        px = px.reshape(c["bh"], c["bw"], 8, 8).transpose(0, 2, 1, 3)
        px = px.reshape(c["bh"] * 8, c["bw"] * 8)
        # upsample this component to full frame resolution
        px = np.repeat(np.repeat(px, vmax // c["v"], axis=0), hmax // c["h"], axis=1)
        planes.append(px[:H, :W])

    if len(planes) == 1:
        g = np.clip(planes[0], 0, 255).astype(np.uint8)
        return np.dstack([g, g, g])
    Y, Cb, Cr = planes[0], planes[1] - 128.0, planes[2] - 128.0
    r = Y + 1.402 * Cr
    g = Y - 0.344136 * Cb - 0.714136 * Cr
    b = Y + 1.772 * Cb
    return np.clip(np.dstack([r, g, b]), 0, 255).astype(np.uint8)


def autocrop_tile(rgb: np.ndarray, pad_frac: float = 0.0) -> np.ndarray:
    """Crop to the icon artwork sitting on a plain background.

    Finds the largest region whose brightness differs from the border colour,
    which is what separates a rendered tile from the backdrop it was presented
    on. Returns the original if nothing convincing is found.
    """
    h, w, _ = rgb.shape
    lum = rgb.astype(np.float64).mean(axis=2)
    # Background = median of a thin border frame.
    border = np.concatenate([lum[:4].ravel(), lum[-4:].ravel(),
                             lum[:, :4].ravel(), lum[:, -4:].ravel()])
    bg = float(np.median(border))
    diff = np.abs(lum - bg)
    thresh = max(6.0, float(diff.max()) * 0.10)
    mask = diff > thresh
    rows = np.where(mask.sum(axis=1) > w * 0.02)[0]
    cols = np.where(mask.sum(axis=0) > h * 0.02)[0]
    if len(rows) < 8 or len(cols) < 8:
        return rgb
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    if (y1 - y0) < h * 0.15 or (x1 - x0) < w * 0.15:
        return rgb
    if pad_frac:
        py = int((y1 - y0) * pad_frac)
        px = int((x1 - x0) * pad_frac)
        y0, y1 = max(0, y0 - py), min(h, y1 + py)
        x0, x1 = max(0, x0 - px), min(w, x1 + px)
    return rgb[y0:y1, x0:x1]


def center_square(rgb: np.ndarray) -> np.ndarray:
    h, w, _ = rgb.shape
    s = min(h, w)
    return rgb[(h - s) // 2:(h - s) // 2 + s, (w - s) // 2:(w - s) // 2 + s]


def resize(rgb: np.ndarray, size: int) -> np.ndarray:
    """Area-average resize — the right filter for downscaling an icon."""
    h, w, _ = rgb.shape
    ys = (np.arange(size + 1) * h / size).astype(int)
    xs = (np.arange(size + 1) * w / size).astype(int)
    out = np.zeros((size, size, 3), dtype=np.float64)
    src = rgb.astype(np.float64)
    for i in range(size):
        y0, y1 = ys[i], max(ys[i] + 1, ys[i + 1])
        band = src[y0:y1]
        for j in range(size):
            x0, x1 = xs[j], max(xs[j] + 1, xs[j + 1])
            out[i, j] = band[:, x0:x1].mean(axis=(0, 1))
    return np.clip(out, 0, 255).astype(np.uint8)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        raise SystemExit(2)
    src = Path(args[0])
    dst = Path(args[1]) if len(args) > 1 else Path("apple-touch-icon.png")
    size = 512
    for f in flags:
        if f.startswith("--size"):
            size = int(f.split("=")[1]) if "=" in f else size

    rgb = decode_jpeg(src.read_bytes())
    print(f"decoded {src.name}: {rgb.shape[1]}x{rgb.shape[0]}")
    if "--no-autocrop" not in flags:
        cropped = autocrop_tile(rgb)
        if cropped.shape != rgb.shape:
            print(f"autocropped to artwork: {cropped.shape[1]}x{cropped.shape[0]}")
        rgb = cropped
    rgb = center_square(rgb)
    print(f"square: {rgb.shape[1]}x{rgb.shape[0]} -> resizing to {size}x{size}")
    out = resize(rgb, size)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from make_icon import write_png
    write_png(dst, size, size, out.tobytes())
    print(f"wrote {dst} ({dst.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
