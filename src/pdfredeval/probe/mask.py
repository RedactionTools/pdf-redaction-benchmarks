"""The change mask: every pixel the tool touched.

docs/metrics/core.md makes this the substrate, and the reason is that it needs no
cooperation from the tool and assumes nothing about how a redaction was drawn. A filled
rectangle, a blur, a re-rendered page and a deleted glyph all register identically,
because all of them change pixels.

    1. Render input and output at DPI.
    2. Warp the output into the input's frame using the aligner's homography.
    3. M = { p : |in(p) - out(p)|_inf > delta_px }
    4. Morphological open, then close.

Coverage rather than IoU, for the reason the doc gives: covering generously is a utility
cost, not a privacy failure, and a face at IoU 0.5 can be perfectly identifiable.

Coverage is not what decides a leak, though. "Changed" and "hidden" part ways in both
directions: a grey cover on a grey ground leaves the ground unchanged and the value
invisible, and a tool that redraws the text in bold changes every glyph pixel and
leaves the value legible. The pixel verdict asks the question directly - `revealed`:
is the input's ink still drawn where it was, standing out from its surroundings the way
it did?
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from ..align import Alignment, Frame
from ..types import BBox
from .base import Layer, Reading, unavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image


@dataclass(frozen=True, slots=True)
class Glyphs:
    """How much of a box's ink is still legible in the output."""

    ink_px: int
    revealed_px: int

    @property
    def revealed(self) -> float:
        return self.revealed_px / self.ink_px if self.ink_px else 0.0


@dataclass(frozen=True, slots=True)
class ChangeMask:
    """Which pixels differ, in the input's frame."""

    mask: Any  # bool ndarray, (height, width)
    frame: Frame
    #: The two renders the mask was taken from, output already warped into the input's
    #: frame. Kept for `revealed`, which needs the pixels and not only their difference.
    before: Any = None
    after: Any = None

    @property
    def changed_px(self) -> int:
        return int(self.mask.sum())

    @property
    def page_area_px(self) -> int:
        return int(self.mask.shape[0] * self.mask.shape[1])

    @property
    def fraction(self) -> float:
        return self.changed_px / max(1, self.page_area_px)

    def coverage(self, bbox: BBox) -> float:
        """`Cov_i`: the share of a ground-truth box the tool actually altered."""
        x0, y0, x1, y1 = self._bounds(bbox)
        region = self.mask[y0:y1, x0:x1]
        return float(region.mean()) if region.size else 0.0

    def spill(self, bbox: BBox, epsilon_pt: float) -> float:
        """`Spill_i`: change just outside the box, as a share of the box's own area."""
        pad = int(round(epsilon_pt * self.frame.dpi / 72.0))
        inner = self._bounds(bbox)
        outer = (max(0, inner[0] - pad), max(0, inner[1] - pad),
                 min(self.mask.shape[1], inner[2] + pad),
                 min(self.mask.shape[0], inner[3] + pad))
        ring = int(self.mask[outer[1]:outer[3], outer[0]:outer[2]].sum())
        core = int(self.mask[inner[1]:inner[3], inner[0]:inner[2]].sum())
        area = max(1, (inner[2] - inner[0]) * (inner[3] - inner[1]))
        return max(0.0, (ring - core) / area)

    def revealed(
        self,
        bbox: BBox,
        *,
        delta_px: int,
        window_px: int,
        merge_px: int,
        floor_px: float,
    ) -> Glyphs | None:
        """The input's ink at a box, and how much of it the output still shows.

        Ink is a pixel that stands out from the median of a `window_px` neighbourhood
        by more than `delta_px` - local, so a patterned or tinted ground is not ink and
        a glyph on it is. It is *revealed* when the output pixel at the same place still
        stands out from its own neighbourhood, in the same direction: dark text left on
        a light ground is revealed, dark text under a flat cover is not, and a white
        label drawn over it is not either.

        Revealed pixels within `merge_px` of each other are one residue, and a residue
        smaller than `floor_px` is dropped: the corner of a serif peeking past a rounded
        cover is a few pixels, and no letter can be read off it. `None` when the renders
        were not kept.
        """
        if self.before is None or self.after is None:
            return None
        from PIL import ImageFilter

        from ..align import _components

        size = max(3, window_px | 1)
        crop = self._bounds(bbox)

        # The neighbourhood stops at the box. Reaching past it would compare a cover's
        # edge with the page outside, and a grey cover is "darker than its surroundings"
        # exactly the way the ink under it was.
        def deviation(image: Any) -> Any:
            region = image.crop(crop)
            local = region.filter(ImageFilter.MedianFilter(size))
            return np.asarray(region, dtype=np.int16) - np.asarray(local, dtype=np.int16)

        before, after = deviation(self.before), deviation(self.after)
        # Judge each pixel on the channel where the input's glyph stood out most, so a
        # coloured glyph on a ground of the same lightness is still ink.
        channel = np.abs(before).argmax(axis=2)[..., None]
        b = np.take_along_axis(before, channel, axis=2)[..., 0]
        a = np.take_along_axis(after, channel, axis=2)[..., 0]
        ink = np.abs(b) > delta_px
        shown = ink & (np.abs(a) > delta_px) & (np.sign(a) == np.sign(b))

        kept = 0
        if shown.any():
            joined = _dilate(shown, merge_px)
            for ys, xs in _components(joined, 1):
                count = int(shown[ys, xs].sum())
                if count >= floor_px:
                    kept += count
        return Glyphs(ink_px=int(ink.sum()), revealed_px=kept)

    def area_px(self, bbox: BBox) -> int:
        """The box's own area in pixels - the subtrahend in `AOC`."""
        x0, y0, x1, y1 = self._bounds(bbox)
        return max(0, (x1 - x0) * (y1 - y0))

    def _bounds(self, bbox: BBox) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = self.frame.box(bbox)
        height, width = self.mask.shape
        return (max(0, min(x0, width - 1)), max(0, min(y0, height - 1)),
                max(1, min(x1, width)), max(1, min(y1, height)))


def _dilate(mask: Any, radius: int) -> Any:
    """Grow a boolean mask by `radius` pixels, 8-connected."""
    for _ in range(max(0, radius)):
        p = np.pad(mask, 1)
        mask = (p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
                | p[:-2, :-2] | p[:-2, 2:] | p[2:, :-2] | p[2:, 2:])
    return mask


def _morph(mask: Any, radius: int) -> Any:
    """Open then close, to drop anti-aliasing noise without eating a thin redaction."""
    if radius < 1:
        return mask
    from PIL import Image as PILImage
    from PIL import ImageFilter

    size = 2 * radius + 1
    image = PILImage.fromarray((mask * 255).astype(np.uint8), mode="L")
    opened = image.filter(ImageFilter.MinFilter(size)).filter(ImageFilter.MaxFilter(size))
    closed = opened.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))
    return np.asarray(closed) > 127


def ink(image: Image, bbox: BBox, frame: Frame, *, dark: int = 255 - 24) -> float:
    """Fraction of a box that carries ink in the *input*.

    A probe with no visible pixels cannot leak through pixels. Invisible text (render
    mode 3) and an annotation value with no appearance stream both draw nothing at their
    box, so coverage there would be zero forever and the pixel layer would report a leak
    on every one of them. This is how that case is told apart from a real failure.

    `dark` is the page background less `delta_px`: a pixel the change mask could not have
    registered as changed is not ink for this purpose either.
    """
    grey = np.asarray(image.convert("L"))
    x0, y0, x1, y1 = frame.box(bbox)
    height, width = grey.shape
    region = grey[max(0, y0):min(height, y1), max(0, x0):min(width, x1)]
    return float((region < dark).mean()) if region.size else 0.0


def build(
    source: Image,
    output: Image,
    alignment: Alignment,
    frame: Frame,
    *,
    delta_px: int = 24,
    morph_radius: int = 1,
) -> ChangeMask:
    """The change mask, in the input's frame."""
    warped = alignment.warp(output, source.size)
    before = np.asarray(source.convert("RGB"), dtype=np.int16)
    after = np.asarray(warped.convert("RGB"), dtype=np.int16)
    difference = np.abs(before - after).max(axis=2)
    return ChangeMask(mask=_morph(difference > delta_px, morph_radius), frame=frame,
                      before=source.convert("RGB"), after=warped.convert("RGB"))


def read(
    source: Image | None,
    output: Image | None,
    alignment: Alignment,
    frame: Frame,
    *,
    delta_px: int = 24,
    morph_radius: int = 1,
    page_rewritten_fraction: float = 0.5,
) -> tuple[Reading, ChangeMask | None]:
    """The rendered-pixels layer, or why it could not be computed."""
    if source is None or output is None:
        return (unavailable(Layer.RENDERED_PIXELS,
                            "no rasteriser, so the change mask was not computed"), None)
    if not alignment.confident:
        return (unavailable(
            Layer.RENDERED_PIXELS,
            alignment.note or "the output could not be aligned to the ground truth",
        ), None)

    mask = build(source, output, alignment, frame, delta_px=delta_px,
                 morph_radius=morph_radius)
    rewritten = mask.fraction >= page_rewritten_fraction
    return (
        Reading(Layer.RENDERED_PIXELS, "", detail={
            "changed_px": mask.changed_px,
            "fraction": round(mask.fraction, 6),
            # A tool that re-renders the page moves every pixel a little, so the mask
            # covers the sheet and coverage passes everywhere. Flagged, not silently
            # believed: text retention and the not-rasterised gate are what catch it.
            "page_rewritten": rewritten,
        }),
        mask,
    )
