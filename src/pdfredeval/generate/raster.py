"""A tiny raster canvas and the image XObject plumbing.

No third-party imaging dependency: samples are a bytearray, and `zlib` from the standard
library provides the `FlateDecode` a PDF image stream needs. Generation stays installable
anywhere, and - more to the point - a seeded case stays byte-identical on every machine,
which a system-dependent rasteriser could not promise.
"""

from __future__ import annotations

import random
import zlib
from dataclasses import dataclass
from typing import Any

from .pdf import Name, PdfWriter, Ref


@dataclass(slots=True)
class Raster:
    """An 8-bit image. `channels` is 1 (grey) or 3 (RGB)."""

    width: int
    height: int
    channels: int = 3
    samples: bytearray = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError(f"degenerate raster {self.width}x{self.height}")
        if self.channels not in (1, 3):
            raise ValueError("channels must be 1 or 3")
        if self.samples is None:
            self.samples = bytearray(b"\xff" * (self.width * self.height * self.channels))

    def _index(self, x: int, y: int) -> int:
        return (y * self.width + x) * self.channels

    def set(self, x: int, y: int, value: tuple[int, ...] | int) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        i = self._index(x, y)
        if isinstance(value, int):
            value = (value,) * self.channels
        for c in range(self.channels):
            self.samples[i + c] = max(0, min(255, value[c]))

    def get(self, x: int, y: int) -> tuple[int, ...]:
        i = self._index(x, y)
        return tuple(self.samples[i + c] for c in range(self.channels))

    def add_noise(self, rng: random.Random, amount: int) -> None:
        """Per-sample uniform noise, the coarse part of a scan's grain."""
        if amount <= 0:
            return
        for i in range(len(self.samples)):
            self.samples[i] = max(0, min(255, self.samples[i] + rng.randint(-amount, amount)))

    def box_blur(self, radius: int = 1) -> None:
        """Separable box blur. Softens value noise into something photograph-like."""
        if radius < 1:
            return
        for _ in range(2):  # two passes approximate a gaussian well enough
            for axis in (0, 1):
                source = bytes(self.samples)
                for y in range(self.height):
                    for x in range(self.width):
                        acc = [0] * self.channels
                        count = 0
                        for d in range(-radius, radius + 1):
                            sx = x + d if axis == 0 else x
                            sy = y if axis == 0 else y + d
                            if not (0 <= sx < self.width and 0 <= sy < self.height):
                                continue
                            j = (sy * self.width + sx) * self.channels
                            for c in range(self.channels):
                                acc[c] += source[j + c]
                            count += 1
                        i = self._index(x, y)
                        for c in range(self.channels):
                            self.samples[i + c] = acc[c] // count

    def to_stream(self) -> tuple[dict[str, Any], bytes]:
        return (
            {
                "Type": Name("XObject"),
                "Subtype": Name("Image"),
                "Width": self.width,
                "Height": self.height,
                "ColorSpace": Name("DeviceRGB" if self.channels == 3 else "DeviceGray"),
                "BitsPerComponent": 8,
                "Filter": Name("FlateDecode"),
            },
            zlib.compress(bytes(self.samples), 9),
        )

    def add_to(self, writer: PdfWriter) -> Ref:
        dictionary, data = self.to_stream()
        return writer.add_stream(dictionary, data)


def photo_ground(width: int, height: int, rng: random.Random) -> Raster:
    """A procedural stand-in for a photograph: interpolated value noise, blurred.

    Not a photograph, and not pretending to be one. What the probe needs is a ground with
    varying luminance and colour so a binariser cannot pick one global threshold, and that
    is exactly what this produces - deterministically, from the case seed.
    """
    raster = Raster(width, height, channels=3)
    cell = max(6, min(width, height) // 4)
    cols, rows = width // cell + 2, height // cell + 2
    grid = [
        [tuple(rng.randint(70, 210) for _ in range(3)) for _ in range(cols)]
        for _ in range(rows)
    ]
    for y in range(height):
        gy, fy = divmod(y, cell)
        ty = fy / cell
        for x in range(width):
            gx, fx = divmod(x, cell)
            tx = fx / cell
            value = []
            for c in range(3):
                top = grid[gy][gx][c] * (1 - tx) + grid[gy][gx + 1][c] * tx
                bottom = grid[gy + 1][gx][c] * (1 - tx) + grid[gy + 1][gx + 1][c] * tx
                value.append(int(top * (1 - ty) + bottom * ty))
            raster.set(x, y, tuple(value))
    raster.add_noise(rng, 14)
    raster.box_blur(1)
    return raster
