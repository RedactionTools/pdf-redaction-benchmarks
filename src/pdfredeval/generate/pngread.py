"""A minimal PNG reader, so the logo embeds without an imaging dependency.

Generation is meant to work from a bare `uv sync`; Pillow is an extra for the rasterised
probe conditions. Decoding one small, known, committed PNG is a few dozen lines of
stdlib, which is a better trade than making the brand mark conditional on an optional
package.

Supports what the committed asset is: 8-bit RGB or RGBA, non-interlaced. Anything else
raises rather than guessing.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


class UnsupportedPNG(ValueError):
    """The file is a PNG this reader deliberately does not handle."""


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def read_rgba(path: Path | str) -> tuple[int, int, bytearray]:
    """Return (width, height, RGBA samples), 4 bytes per pixel."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise UnsupportedPNG(f"{path} is not a PNG")

    pos, idat, header = 8, bytearray(), None
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + length

    if header is None:
        raise UnsupportedPNG(f"{path} has no IHDR")
    width, height, depth, colour, _, _, interlace = header
    if depth != 8 or colour not in (2, 6) or interlace != 0:
        raise UnsupportedPNG(
            f"{path}: only 8-bit RGB/RGBA without interlacing is supported "
            f"(got depth={depth}, colour type={colour}, interlace={interlace})"
        )

    channels = 4 if colour == 6 else 3
    stride = width * channels
    raw = zlib.decompress(bytes(idat))
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise UnsupportedPNG(f"{path}: expected {expected} filtered bytes, got {len(raw)}")

    out = bytearray(height * stride)
    previous = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        filt = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        if filt == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filt == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filt == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filt == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upleft = previous[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, previous[i], upleft)) & 0xFF
        elif filt != 0:
            raise UnsupportedPNG(f"{path}: unknown filter {filt} on row {y}")
        out[y * stride : (y + 1) * stride] = line
        previous = line

    if channels == 4:
        return width, height, out
    rgba = bytearray(width * height * 4)
    for i in range(width * height):
        rgba[i * 4 : i * 4 + 3] = out[i * 3 : i * 3 + 3]
        rgba[i * 4 + 3] = 255
    return width, height, rgba


def flatten(width: int, height: int, rgba: bytearray, background: float) -> bytearray:
    """Composite RGBA over a flat grey, returning RGB.

    The banner has no transparency to preserve, and a flattened image needs no SMask -
    one less object in a file whose structure is itself under test.
    """
    bg = max(0, min(255, round(background * 255)))
    rgb = bytearray(width * height * 3)
    for i in range(width * height):
        alpha = rgba[i * 4 + 3]
        for c in range(3):
            src = rgba[i * 4 + c]
            rgb[i * 3 + c] = (src * alpha + bg * (255 - alpha)) // 255
    return rgb
