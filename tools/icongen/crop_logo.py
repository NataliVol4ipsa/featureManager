"""One-off: crop the transparent margins off the VS Code product logo in icons.py.

The embedded VS Code PNG (70x70 RGBA) carries a wide transparent border, so the
visible glyph looked small in the toolbar. This trims to the opaque bounding box
and rewrites both ACTION_ICON_DARK/LIGHT entries in ../../icons.py. Pure stdlib
(zlib) - no PIL/Node needed. Run once from tools/icongen: python crop_logo.py
"""
import base64
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
import icons as cur

LABEL = "Open workspace in VS Code"
ALPHA_THRESHOLD = 8  # pixels with alpha at/below this count as transparent margin
TARGET = 18  # final square px size, matching the Lucide toolbar icons


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_rgba(png):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width = height = None
    idat = bytearray()
    pos = 8
    while pos < len(png):
        length = struct.unpack(">I", png[pos:pos + 4])[0]
        ctype = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", data[:10])
            assert depth == 8 and color == 6, (depth, color)
        elif ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
        pos += 12 + length

    raw = zlib.decompress(bytes(idat))
    bpp = 4
    stride = width * bpp
    out = bytearray(height * stride)
    prev = bytearray(stride)
    src = 0
    for y in range(height):
        ftype = raw[src]
        src += 1
        line = bytearray(raw[src:src + stride])
        src += stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 0xFF
            elif ftype == 2:
                line[x] = (line[x] + b) & 0xFF
            elif ftype == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
            elif ftype == 4:
                line[x] = (line[x] + _paeth(a, b, c)) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, out


def encode_rgba(width, height, pixels):
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += pixels[y * stride:(y + 1) * stride]
    comp = zlib.compress(bytes(raw), 9)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", comp) + chunk(b"IEND", b""))


def crop(png_b64):
    png = base64.b64decode(png_b64)
    w, h, px = decode_rgba(png)
    stride = w * 4
    min_x, min_y, max_x, max_y = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if px[y * stride + x * 4 + 3] > ALPHA_THRESHOLD:
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
    if max_x < 0:
        raise SystemExit("logo is fully transparent?!")

    cw, ch = max_x - min_x + 1, max_y - min_y + 1
    cropped = bytearray(cw * ch * 4)
    for y in range(ch):
        srow = (min_y + y) * stride + min_x * 4
        cropped[y * cw * 4:(y + 1) * cw * 4] = px[srow:srow + cw * 4]
    print(f"cropped {w}x{h} -> {cw}x{ch} (box {min_x},{min_y}..{max_x},{max_y})")
    return cw, ch, cropped


def resize(w, h, px, size):
    """Area-average downscale to size x size, alpha-premultiplied for clean edges."""
    dst = bytearray(size * size * 4)
    for dy in range(size):
        sy0, sy1 = dy * h / size, (dy + 1) * h / size
        for dx in range(size):
            sx0, sx1 = dx * w / size, (dx + 1) * w / size
            r = g = b = a = area = 0.0
            iy = int(sy0)
            while iy < sy1:
                wy = min(iy + 1, sy1) - max(iy, sy0)
                ix = int(sx0)
                while ix < sx1:
                    wx = min(ix + 1, sx1) - max(ix, sx0)
                    cov = wx * wy
                    o = (iy * w + ix) * 4
                    pa = px[o + 3] / 255.0
                    r += px[o] * pa * cov
                    g += px[o + 1] * pa * cov
                    b += px[o + 2] * pa * cov
                    a += px[o + 3] * cov
                    area += cov
                    ix += 1
                iy += 1
            d = (dy * size + dx) * 4
            alpha = a / area if area else 0.0
            pa = alpha / 255.0
            if pa > 0:
                dst[d] = min(255, round(r / area / pa))
                dst[d + 1] = min(255, round(g / area / pa))
                dst[d + 2] = min(255, round(b / area / pa))
            dst[d + 3] = min(255, round(alpha))
    return dst


def main():
    cw, ch, cropped = crop(cur.ACTION_ICON_DARK[LABEL])
    resized = resize(cw, ch, cropped, TARGET)
    new_b64 = base64.b64encode(encode_rgba(TARGET, TARGET, resized)).decode()
    print(f"resized {cw}x{ch} -> {TARGET}x{TARGET}")
    path = os.path.join(ROOT, "icons.py")
    text = open(path, encoding="utf-8").read()

    def wrapped(b64):
        import textwrap
        lines = textwrap.wrap(b64, 76)
        return "(\n" + "".join(f'        "{c}"\n' for c in lines) + "    )"

    # Replace both DARK and LIGHT entries for the VS Code label.
    pattern = re.compile(
        r'(\s*' + re.escape(repr(LABEL)) + r':\s*)\((?:\s*"[^"]*"\s*)+\)',
    )
    replacement = lambda m: m.group(1) + wrapped(new_b64)
    text, n = pattern.subn(replacement, text)
    assert n == 2, f"expected 2 VS Code entries, replaced {n}"
    open(path, "w", encoding="utf-8").write(text)
    print(f"rewrote {n} VS Code logo entries in icons.py")


if __name__ == "__main__":
    main()
