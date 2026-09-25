"""Rendering text to pixels, for probes whose value exists only as an image.

These probes carry no text layer at all, so the only way a tool can reach them is OCR -
which is exactly what `Reach` for the `image_text` channel measures.

Fonts are bundled (see `fonts/README.md`) rather than discovered on the host, because a
seeded case must be byte-identical everywhere. Pillow is an optional dependency: vector
generation never needs it, and `require_backend()` says so plainly when it is missing.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .raster import Raster

FONT_DIR = Path(__file__).parent / "fonts"

#: provenance -> bundled face. `hand-mixed` shares the joined script; its printed labels
#: are drawn separately as vector text.
FONT_FOR_PROVENANCE = {
    "print-clean": "RobotoMono.ttf",
    "print-degraded": "RobotoMono.ttf",
    "hand-block": "Caveat.ttf",
    "hand-cursive": "DancingScript.ttf",
    "hand-mixed": "DancingScript.ttf",
}

#: Rendered at this multiple of the nominal point size, then placed back at the right
#: physical size. 300 DPI is the resolution the scoring pipeline renders output at.
RENDER_DPI = 300.0
PT_PER_INCH = 72.0


class BackendMissing(RuntimeError):
    """Pillow is not installed, so raster conditions cannot be drawn."""


def have_backend() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def require_backend() -> None:
    if not have_backend():
        raise BackendMissing(
            "rasterised conditions need Pillow: install it with "
            "`uv sync --extra generate` (or `pip install 'pdfredeval[generate]'`)"
        )


@lru_cache(maxsize=8)
def font_digest(filename: str) -> str:
    """SHA-256 of a bundled face, recorded in ground truth.

    A disputed OCR result should be able to name the exact glyphs it was produced from.
    """
    return hashlib.sha256((FONT_DIR / filename).read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class RenderedText:
    raster: Raster
    width_pt: float
    height_pt: float
    font_file: str
    font_sha256: str

    def provenance(self) -> dict[str, Any]:
        return {"font": self.font_file, "font_sha256": self.font_sha256}


def _degrade(raster: Raster, rng: random.Random) -> None:
    """What a mediocre scan does to a page.

    Ordered the way the physical process orders it: ink bleeds, the sensor adds grain,
    the optics soften, and the encoder quantises.
    """
    # Bleed-through: faint ghosting offset by a pixel or two.
    source = bytes(raster.samples)
    for y in range(raster.height):
        for x in range(raster.width):
            j = ((y + 2) * raster.width + (x + 1)) * raster.channels
            if j + raster.channels <= len(source):
                i = (y * raster.width + x) * raster.channels
                for c in range(raster.channels):
                    ghost = 255 - (255 - source[j + c]) // 4
                    raster.samples[i + c] = min(raster.samples[i + c], ghost)
    raster.add_noise(rng, 26)
    raster.box_blur(1)
    # Speckle: isolated dropouts and dark flecks, which binarisers handle badly.
    for _ in range(max(4, raster.width * raster.height // 400)):
        x, y = rng.randrange(raster.width), rng.randrange(raster.height)
        raster.set(x, y, 0 if rng.random() < 0.5 else 255)
    # Coarse quantisation, standing in for aggressive JPEG.
    for i in range(len(raster.samples)):
        raster.samples[i] = (raster.samples[i] // 24) * 24


def render_text(
    content: str,
    *,
    provenance: str,
    size_pt: float,
    rng: random.Random,
    inverse: bool = False,
    low_contrast: bool = False,
) -> RenderedText:
    """Rasterise `content` and return it with the physical size it should occupy."""
    require_backend()
    from PIL import Image, ImageDraw, ImageFont

    filename = FONT_FOR_PROVENANCE[provenance]
    scale = RENDER_DPI / PT_PER_INCH
    px = max(8, int(round(size_pt * scale)))
    font = ImageFont.truetype(str(FONT_DIR / filename), px)

    pad = max(4, px // 4)
    box = font.getbbox(content)
    # getbbox returns floats; ceil rather than round, so no glyph edge is clipped.
    width = math.ceil(box[2] - box[0]) + pad * 2
    height = math.ceil(box[3] - box[1]) + pad * 2

    background, ink = (16, 235) if inverse else (255, 0)
    if low_contrast and not inverse:
        background, ink = 158, 128

    image = Image.new("L", (width, height), background)
    draw = ImageDraw.Draw(image)

    if provenance.startswith("hand-"):
        # Per-glyph jitter, so the run is not a rigid baseline of identical letterforms.
        x = float(pad - box[0])
        baseline = float(pad - box[1])
        for ch in content:
            dx = rng.uniform(-px * 0.02, px * 0.02)
            dy = rng.uniform(-px * 0.05, px * 0.05)
            draw.text((x + dx, baseline + dy), ch, font=font, fill=ink)
            advance = draw.textlength(ch, font=font)
            x += advance + rng.uniform(-px * 0.015, px * 0.01)
        width = max(width, int(x) + pad)
        image = image.crop((0, 0, width, height))
    else:
        draw.text((pad - box[0], pad - box[1]), content, font=font, fill=ink)

    raster = Raster(image.width, image.height, channels=1,
                    samples=bytearray(image.tobytes()))
    if provenance == "print-degraded":
        _degrade(raster, rng)

    return RenderedText(
        raster=raster,
        width_pt=raster.width / scale,
        height_pt=raster.height / scale,
        font_file=filename,
        font_sha256=font_digest(filename),
    )
