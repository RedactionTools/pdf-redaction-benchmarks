"""CaseBuilder: draws a page, records ground truth, emits `<case_id>.pdf` + `ground_truth.json`.

Every probe added here returns a `Probe` whose bbox is the exact box of the glyphs drawn,
because coverage is scored against that box. The builder refuses to emit a case that
violates an invariant the scorer depends on - lexical uniqueness, a valid xref, a probe
without a scoreable unit - since a silently malformed case corrupts every score computed
from it.
"""

from __future__ import annotations

import json
import random
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .._version import __version__ as _package_version
from ..textmatch import disclosure
from ..types import BBox, Case, Channel, Conditions, Difficulty, Probe, ProbeKind
from ..workspace import LEGACY_CASE_PDF, case_pdf_name
from .branding import (
    PROJECT_URL,
    REPO_URL,
    brand_strings,
    draw_banner,
    pdf_date,
    resolve_generated_at,
)
from .layout import PageLayout, Slot
from .pdf import (
    COURIER,
    COURIER_BOLD,
    FONTS,
    WINANSI,
    Font,
    Matrix,
    Name,
    PdfWriter,
    Stream,
    aabb,
    check_xref,
    escape_show_string,
)
from .raster import Raster, photo_ground
from .textraster import (
    have_backend,
    render_text,
)
from .values import Value

#: Stamped into the banner, the metadata and the ground truth of every case.
GENERATOR_VERSION = _package_version

#: Conditions that need a rasteriser; declared so a family can skip them with a reason
#: rather than silently emitting something that does not exercise the condition.
#: Needs glyphs rendered to pixels, which needs a font. `on-image` is deliberately not
#: here: it means vector text over a photographic ground, so only the background is
#: rasterised and no font is involved.
RASTER_CONDITIONS = {
    "provenance": {"print-clean", "print-degraded", "hand-block", "hand-cursive", "hand-mixed"},
    "polarity": set(),
}


class UnsupportedCondition(NotImplementedError):
    """This condition needs a backend the generator does not have yet."""


SCALE_SIZE = {"pt10": 10.0, "pt6": 6.0, "pt4": 4.0}

#: Caption line above each probe. Small enough that two lines fit a 22pt row.
LABEL_SIZE = 6.4
INDEX_SIZE = 5.4
#: Runs that start at the top of their slot - stacked, or turned to read downwards -
#: would otherwise begin exactly where the caption sits.
CAPTION_CLEARANCE = 13.0
#: Line spacing for stacked vertical runs, as a multiple of the size.
STACK_LEADING = 1.05


def probe_width(value_text: str, label: str | None, size: float = 10.0) -> float:
    """How wide a packed probe needs to be: whichever of its two lines is longer."""
    caption = COURIER_BOLD.width(f"{label}:", LABEL_SIZE) + 3.5 if label else 0.0
    caption += COURIER.width("[t000]", INDEX_SIZE)
    return max(COURIER.width(value_text, size), caption) + 2.0


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


@dataclass
class CaseBuilder:
    case_id: str
    seed: int
    family: str
    layout: PageLayout = field(default_factory=PageLayout)
    dataset_revision: str | None = None
    #: UTC ISO-8601. Left unset it follows SOURCE_DATE_EPOCH, then the clock - see
    #: `branding.resolve_generated_at`.
    generated_at: str | None = None

    _ops: list[str] = field(default_factory=list, init=False)
    _probes: list[Probe] = field(default_factory=list, init=False)
    _annots: list[dict[str, Any]] = field(default_factory=list, init=False)
    _info: dict[str, str] = field(default_factory=dict, init=False)
    _xmp_extra: list[tuple[str, str]] = field(default_factory=list, init=False)
    _ocgs: list[tuple[str, str]] = field(default_factory=list, init=False)
    _prior_revision: tuple[str, int] | None = field(default=None, init=False)
    _tj_index: int = field(default=-1, init=False)
    _rendered: list[str] = field(default_factory=list, init=False)
    #: Caption boxes (`Label: [id]` lines), by the probe they head. No probe box may
    #: reach into one: the scorer reads a box's pixels as the value's, and a caption
    #: inside it is ink no tool should remove - so it would read as the value showing.
    _captions: list[tuple[str, tuple[float, float, float, float]]] = field(
        default_factory=list, init=False)
    _planted: dict[str, tuple[bytes, ...]] = field(default_factory=dict, init=False)
    _images: list[tuple[str, Raster]] = field(default_factory=list, init=False)
    _fonts_used: dict[str, str] = field(default_factory=dict, init=False)
    _counter: int = field(default=0, init=False)
    #: Set by `reserve_with`. Non-probe text drawn on the page is fed back to the
    #: value factory, so a value drawn later cannot collide with a label, a section
    #: title or a line of boilerplate already on the sheet.
    _reserve: Callable[[str], None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.generated_at = resolve_generated_at(self.generated_at)
        # Drawn first, so it sits under the probes in z-order and is included in the
        # rendered-text set that `validate()` checks probe units against.
        draw_banner(
            self,
            self.layout.brand_slot(),
            case_id=self.case_id,
            version=GENERATOR_VERSION,
            generated_at=self.generated_at,
        )

    @property
    def brand_strings(self) -> tuple[str, ...]:
        """Text a probe value must not collide with; families reserve these."""
        return brand_strings(self.case_id, GENERATOR_VERSION, self.generated_at or "")

    def reserve_with(self, factory: Any) -> None:
        """Register every non-probe string this builder draws with `factory`.

        The scorer hunts each disclosure unit across the whole output, so anything
        printed on the page is something a probe value must not share a long run
        with. Wiring the two together here means a family cannot forget one.
        """
        self._reserve = factory.reserve_text

    def _register(self, text: str) -> None:
        if self._reserve is not None:
            self._reserve(text)

    # --- low-level drawing ---------------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter:03d}"

    def _record_planted(self, probe_id: str, value: str, *, per_glyph: bool = False) -> None:
        """Record the byte patterns that prove this probe was actually written.

        Stacked vertical text emits one `Tj` per glyph, so the value never appears as a
        contiguous literal. That is correct rendering, not a defect - but it means the
        planted-value check has to know how each probe was laid down.
        """
        if per_glyph:
            self._planted[probe_id] = tuple(
                escape_show_string(ch) + b" Tj" for ch in value
            )
        else:
            self._planted[probe_id] = (value.encode(WINANSI, errors="replace"),)

    def rect(self, x: float, y: float, w: float, h: float,
             *, gray: float | None = None, rgb: tuple[float, float, float] | None = None,
             fill: bool = True) -> None:
        self._ops.append("q")
        if rgb is not None:
            self._ops.append(f"{_fmt(rgb[0])} {_fmt(rgb[1])} {_fmt(rgb[2])} rg")
        elif gray is not None:
            self._ops.append(f"{_fmt(gray)} g")
        self._ops.append(f"{_fmt(x)} {_fmt(y)} {_fmt(w)} {_fmt(h)} re {'f' if fill else 'S'}")
        self._ops.append("Q")

    def image(self, raster: Raster, x: float, y: float, w: float, h: float,
              *, matrix: Matrix | None = None) -> str:
        """Place a raster in page coordinates; returns its resource name.

        A PDF image always occupies the unit square, so its placement *is* a matrix -
        which makes a rotated scan no harder to draw than an upright one.
        """
        name = f"Im{len(self._images)}"
        self._images.append((name, raster))
        place = matrix if matrix is not None else Matrix(a=w, d=h, e=x, f=y)
        self._ops.append("q")
        self._ops.append(f"{place.as_operands()} cm")
        self._ops.append(f"/{name} Do")
        self._ops.append("Q")
        return name

    def text(
        self,
        content: str,
        x: float,
        y: float,
        *,
        font: Font = COURIER,
        size: float = 10.0,
        gray: float | None = None,
        rgb: tuple[float, float, float] | None = None,
        render_mode: int = 0,
        matrix: Matrix | None = None,
        record: bool = True,
        ocg: str | None = None,
    ) -> tuple[float, float, float, float]:
        """Draw a text run; return its axis-aligned bbox in page coordinates.

        `matrix` transforms the run, so the returned box is the AABB of the transformed
        corners - which is what a redaction's coverage is measured against.
        """
        width = font.width(content, size)
        x0, y0 = 0.0, -font.descent * size
        x1, y1 = width, font.ascent * size
        place = Matrix.translate(x, y) if matrix is None else matrix
        corners = [place.apply(px, py) for px, py in
                   ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]

        self._ops.append("q")
        if ocg is not None:
            self._ops.append(f"/OC /{ocg} BDC")
        # Always explicit: a run that inherits its colour renders in whatever the
        # previous operator left behind, which is invisible if that was white.
        if rgb is not None:
            self._ops.append(f"{_fmt(rgb[0])} {_fmt(rgb[1])} {_fmt(rgb[2])} rg")
        else:
            self._ops.append(f"{_fmt(0.0 if gray is None else gray)} g")
        self._ops.append("BT")
        if render_mode:
            self._ops.append(f"{render_mode} Tr")
        self._ops.append(f"/{font.resource} {_fmt(size)} Tf")
        self._ops.append(f"{place.as_operands()} Tm")
        self._tj_index = len(self._ops)
        self._ops.append(escape_show_string(content).decode("latin-1") + " Tj")
        self._ops.append("ET")
        if ocg is not None:
            self._ops.append("EMC")
        self._ops.append("Q")
        if record:
            self._rendered.append(content)
        return aabb(corners)

    def _text_vertical(self, content: str, x: float, y: float, *, font: Font, size: float,
                       gray: float | None = None) -> tuple[float, float, float, float]:
        """Stacked top-to-bottom with upright glyphs.

        Not a rotation: each glyph keeps its orientation and advances downward, so a tool
        that recovers rot90 by rotating the raster gains nothing here.
        """
        # Just enough to separate the glyphs: a long stacked name has to fit the band,
        # and every extra point of leading costs a character's worth of height.
        step = size * STACK_LEADING
        boxes = []
        for i, ch in enumerate(content):
            boxes.append(self.text(ch, x, y - i * step, font=font, size=size, gray=gray,
                                   record=False))
        self._rendered.append(content)
        return aabb([(b[0], b[1]) for b in boxes] + [(b[2], b[3]) for b in boxes])

    # --- conditions ----------------------------------------------------------------

    @staticmethod
    def check_renderable(conditions: Conditions) -> str | None:
        """Why this condition cannot be drawn yet, or None."""
        if conditions.provenance in RASTER_CONDITIONS["provenance"]:
            if not have_backend():
                return (
                    f"provenance {conditions.provenance!r} renders glyphs to pixels, "
                    f"which needs Pillow: `uv sync --extra generate`"
                )
            if conditions.orientation == "vertical":
                return (
                    f"provenance {conditions.provenance!r} with stacked vertical layout "
                    f"is not implemented: a rasterised run is one image, not one per glyph"
                )
        if conditions.polarity in RASTER_CONDITIONS["polarity"]:
            return f"polarity {conditions.polarity!r} needs an image backend"
        return None

    def _render_raster(
        self, content: str, slot: Slot, conditions: Conditions, size: float
    ) -> tuple[float, float, float, float]:
        """Draw the run as pixels. The probe then carries no text layer at all."""
        x, y = slot.baseline
        rng = random.Random(f"{self.seed}:{self._counter}:{content}")

        if conditions.provenance == "hand-mixed":
            # A form: the label is printed, the value is written in by hand.
            label_box = self.text("Name:", x, y, font=COURIER_BOLD, size=size * 0.8,
                                  gray=0.15, record=False)
            x = label_box[2] + size * 0.6

        rendered = render_text(
            content,
            provenance=conditions.provenance,
            size_pt=size,
            rng=rng,
            inverse=conditions.polarity == "inverse",
            low_contrast=conditions.polarity == "low-contrast",
        )
        self._fonts_used[rendered.font_file] = rendered.font_sha256
        w, h = rendered.width_pt, rendered.height_pt

        orient = conditions.orientation
        if orient == "horizontal":
            box = (x, y - size * 0.25, x + w, y - size * 0.25 + h)
            matrix = None
        else:
            angle = {"rot90": 90.0, "rot270": -90.0, "rot180": 180.0, "skew": 15.0}[orient]
            top = slot.y + slot.height - CAPTION_CLEARANCE
            if orient == "rot90":
                ax, ay = slot.x + h, top - w
            elif orient == "rot270":
                ax, ay = slot.x, top
            elif orient == "rot180":
                ax, ay = x + w, y + h
            else:
                ax, ay = x, y - size * 0.25
            matrix = (Matrix(a=w, d=h)
                      .then(Matrix.rotate(angle))
                      .then(Matrix.translate(ax, ay)))
            matrix, box = self._nudge_inside(
                matrix, ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)), slot.x, top)

        # Grounds that are not part of the glyphs themselves still go behind the image.
        if conditions.polarity in ("highlighted", "screened", "on-image"):
            self._paint_ground(conditions.polarity, box, size)
        self.image(rendered.raster, box[0], box[1], w, h, matrix=matrix)
        return box

    @staticmethod
    def _nudge_inside(
        matrix: Matrix, corners: Sequence[tuple[float, float]], left: float, top: float,
    ) -> tuple[Matrix, tuple[float, float, float, float]]:
        """Shift a transformed run right of `left` and down below `top`.

        Rotating a box about a point on its baseline swings the far corner outside the
        column - by `height x sin(angle)` for a skew, which is small, silent, and puts
        the run outside the content area the aligner works in. The same swing lifts the
        far end of a long skewed run into the caption above it.
        """
        box = aabb([matrix.apply(u, v) for u, v in corners])
        dx = max(0.0, left - box[0])
        dy = min(0.0, top - box[3])
        if dx == 0 and dy == 0:
            return matrix, box
        matrix = matrix.then(Matrix.translate(dx, dy))
        return matrix, (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)

    def _paint_ground(self, polarity: str, box: tuple[float, float, float, float],
                      size: float) -> None:
        """Paint the polarity background for an already-measured run."""
        x0, y0, x1, y1 = box
        pad = 2.0
        w, h = x1 - x0 + 2 * pad, y1 - y0 + 2 * pad
        if polarity == "inverse":
            self.rect(x0 - pad, y0 - pad, w, h, gray=0.05)
        elif polarity == "low-contrast":
            self.rect(x0 - pad, y0 - pad, w, h, gray=0.62)
        elif polarity == "highlighted":
            self.rect(x0 - pad, y0 - pad, w, h, rgb=(1.0, 0.92, 0.23))
        elif polarity == "on-image":
            # ~150 DPI is ample: the ground is deliberately soft, and a sharper one would
            # only cost bytes.
            px_w, px_h = max(8, int(w * 150 / 72)), max(8, int(h * 150 / 72))
            rng = random.Random(f"{self.seed}:{x0:.2f}:{y0:.2f}")
            self.image(photo_ground(px_w, px_h, rng), x0 - pad, y0 - pad, w, h)
        elif polarity == "screened":
            # Scaled to the cell, not to the type size: a watermark wider than its own
            # probe runs across the neighbours and puts a condition on cells that were
            # never meant to carry it.
            mark = "CONFIDENTIAL"
            fitted = min(size * 1.6, w / (len(mark) * COURIER_BOLD.char_width))
            self.text(mark, x0 - pad, y0, font=COURIER_BOLD, size=fitted, gray=0.85,
                      record=False)

    def _render_conditioned(
        self, content: str, slot: Slot, conditions: Conditions, font: Font
    ) -> tuple[float, float, float, float]:
        reason = self.check_renderable(conditions)
        if reason:
            raise UnsupportedCondition(reason)

        size = SCALE_SIZE[conditions.scale]
        x, y = slot.baseline

        if conditions.provenance in RASTER_CONDITIONS["provenance"]:
            return self._render_raster(content, slot, conditions, size)

        gray = {"inverse": 1.0, "low-contrast": 0.50}.get(conditions.polarity)

        # Draw the glyphs first so the ground can be sized from their real bounding box.
        # Painting the ground in unrotated coordinates would leave rotated or stacked
        # text sitting beside its background - the condition would not be exercised at
        # all, and inverse text would land white-on-white.
        start = len(self._ops)
        orient = conditions.orientation
        if orient == "vertical":
            top = slot.y + slot.height - CAPTION_CLEARANCE
            box = self._text_vertical(
                content, x, top - font.ascent * size,
                font=font, size=size, gray=gray)
        elif orient == "horizontal":
            box = self.text(content, x, y, font=font, size=size, gray=gray)
        else:
            width = font.width(content, size)
            angle = {"rot90": 90.0, "rot270": -90.0, "rot180": 180.0, "skew": 15.0}[orient]
            # Anchor by the direction the run travels, not by the text baseline: a
            # quarter turn sends the glyphs up or down out of the slot entirely, and a
            # run that leaves its slot lands on a neighbour or outside the content area.
            # Turned runs all hang from one top edge. Anchoring each by the direction
            # it travels leaves a bottom-up run sitting low and a top-down one high, so
            # a band of them reads as ragged rather than as a set to compare.
            top = slot.y + slot.height - CAPTION_CLEARANCE
            if orient == "rot90":  # runs upward: place its far end first
                anchor_x, anchor_y = slot.x + size * 0.75, top - width
            elif orient == "rot270":  # runs downward
                anchor_x, anchor_y = slot.x + size * 0.25, top
            elif orient == "rot180":  # runs leftward
                anchor_x, anchor_y = x + width, y + size
            else:  # skew keeps a near-horizontal baseline
                anchor_x, anchor_y = x, y
            matrix = Matrix.rotate(angle).then(Matrix.translate(anchor_x, anchor_y))
            local = ((0.0, -font.descent * size), (width, -font.descent * size),
                     (width, font.ascent * size), (0.0, font.ascent * size))
            matrix, _ = self._nudge_inside(matrix, local, slot.x, top)
            box = self.text(content, 0, 0, font=font, size=size, gray=gray, matrix=matrix)

        glyph_ops = self._ops[start:]
        del self._ops[start:]
        self._paint_ground(conditions.polarity, box, size)
        self._ops.extend(glyph_ops)
        return box

    # --- probes --------------------------------------------------------------------

    def caption(self, slot: Slot, probe_id: str, label: str | None = None) -> float:
        """Draw the `Label: [id]` line above a probe; return where it ends."""
        y = slot.y + slot.height - LABEL_SIZE - 1.0
        cursor = slot.x
        if label:
            self._register(label)
            box = self.text(label, cursor, y, font=COURIER_BOLD, size=LABEL_SIZE,
                            gray=0.28)
            self._captions.append((probe_id, box))
            cursor = box[2] + 3.5
        end = self.text(f"[{probe_id}]", cursor, y, font=COURIER, size=INDEX_SIZE,
                        gray=0.55, record=False)
        self._captions.append((probe_id, end))
        return end[2]

    def add_text_probe(
        self,
        value: Value,
        slot: Slot,
        *,
        conditions: Conditions | None = None,
        difficulty: Difficulty = Difficulty.EASY,
        label: bool = True,
        trap: str | None = None,
        font: Font = COURIER,
    ) -> Probe:
        conditions = conditions or Conditions()
        probe_id = self._next_id("t")
        # Label and index share one line directly above the value, so a probe costs two
        # short lines rather than a grid cell sized for the longest value on the page.
        show_label = label and value.label and conditions.orientation == "horizontal"
        self.caption(slot, probe_id, f"{value.label}:" if show_label else None)
        box = self._render_conditioned(value.text, slot, conditions, font)
        rasterised = conditions.provenance in RASTER_CONDITIONS["provenance"]
        if rasterised:
            # The value exists as pixels only: nothing to find in the byte stream but the
            # image that carries it.
            self._planted[probe_id] = (f"/{self._images[-1][0]} Do".encode(),)
        else:
            self._record_planted(probe_id, value.text,
                                 per_glyph=conditions.orientation == "vertical")
        probe = Probe(
            id=probe_id,
            kind=ProbeKind.TEXT_SPAN,
            must_redact=value.must_redact,
            severity=value.severity,
            category=value.category,
            value=value.text,
            units=value.units,
            ambiguous=value.ambiguous,
            rationale=value.rationale,
            bbox=BBox(0, *box),
            channel=Channel.IMAGE_TEXT if rasterised else Channel.TEXT_LAYER,
            difficulty=difficulty,
            trap=trap,
            conditions=conditions,
        )
        self._probes.append(probe)
        return probe

    def add_metadata_probe(self, value: Value, *, key: str = "Keywords",
                           xmp: bool = False) -> Probe:
        """A value reachable only through metadata - never drawn on the page.

        If this survives, the tool never opened the metadata channel.
        """
        if xmp:
            self._xmp_extra.append((key, value.text))
        else:
            self._info[key] = value.text
        probe_id = self._next_id("m")
        self._record_planted(probe_id, value.text)
        probe = Probe(
            id=probe_id,
            kind=ProbeKind.METADATA_KEY,
            must_redact=value.must_redact,
            severity=value.severity,
            category=value.category,
            value=value.text,
            units=value.units,
            ambiguous=value.ambiguous,
            rationale=value.rationale,
            channel=Channel.METADATA,
            trap="xmp" if xmp else "info_dict",
        )
        self._probes.append(probe)
        return probe

    def add_invisible_probe(self, value: Value, slot: Slot) -> Probe:
        """Render mode 3: extractable, invisible. A leak no visual review catches."""
        probe_id = self._next_id("i")
        self.caption(slot, probe_id, "Invisible:")
        size = 10.0
        x, y = slot.baseline
        box = self.text(value.text, x, y, size=size, render_mode=3)
        self._record_planted(probe_id, value.text)
        probe = Probe(
            id=probe_id, kind=ProbeKind.TEXT_SPAN, must_redact=value.must_redact,
            severity=value.severity, category=value.category, value=value.text,
            units=value.units,
            ambiguous=value.ambiguous,
            rationale=value.rationale,
            bbox=BBox(0, *box), channel=Channel.TEXT_LAYER,
            difficulty=Difficulty.HARD, trap="invisible_text",
        )
        self._probes.append(probe)
        return probe

    def add_annotation_probe(self, value: Value, slot: Slot) -> Probe:
        """A value in an annotation's /Contents: outside the page content stream."""
        probe_id = self._next_id("a")
        self.caption(slot, probe_id, "Annotation:")
        x, y = slot.baseline
        width = max(60.0, min(slot.width, COURIER.width(value.text, 8.0) + 6.0))
        self.rect(x, y - 3, width, 13, gray=0.93)
        self._annots.append({
            "Type": Name("Annot"), "Subtype": Name("FreeText"),
            "Rect": [x, y - 3, x + width, y + 10],
            "Contents": value.text, "F": 4, "DA": "/Helv 8 Tf 0 g",
        })
        self._record_planted(probe_id, value.text)
        probe = Probe(
            id=probe_id, kind=ProbeKind.TEXT_SPAN, must_redact=value.must_redact,
            severity=value.severity, category=value.category, value=value.text,
            units=value.units,
            ambiguous=value.ambiguous,
            rationale=value.rationale,
            bbox=BBox(0, x, y - 3, x + width, y + 10), channel=Channel.TEXT_LAYER,
            difficulty=Difficulty.HARD, trap="annotation",
        )
        self._probes.append(probe)
        return probe

    def add_hidden_layer_probe(self, value: Value, slot: Slot) -> Probe:
        """A value inside an optional-content group that is off by default."""
        probe_id = self._next_id("o")
        ocg_name = f"MC{len(self._ocgs)}"
        self._ocgs.append((ocg_name, f"hidden-{probe_id}"))
        self.caption(slot, probe_id, "Hidden layer:")
        x, y = slot.baseline
        box = self.text(value.text, x, y, size=10.0, ocg=ocg_name)
        self._record_planted(probe_id, value.text)
        probe = Probe(
            id=probe_id, kind=ProbeKind.TEXT_SPAN, must_redact=value.must_redact,
            severity=value.severity, category=value.category, value=value.text,
            units=value.units,
            ambiguous=value.ambiguous,
            rationale=value.rationale,
            bbox=BBox(0, *box), channel=Channel.TEXT_LAYER,
            difficulty=Difficulty.HARD, trap="optional_content",
        )
        self._probes.append(probe)
        return probe

    def add_prior_revision_probe(self, value: Value, slot: Slot) -> Probe:
        """Plant the value in an earlier revision, overwritten in the current one.

        Tests whether redaction reaches into the input's own history. Only one per case:
        a single incremental update carries it.
        """
        if self._prior_revision is not None:
            raise ValueError("a case can carry only one prior-revision probe")
        probe_id = self._next_id("r")
        self.caption(slot, probe_id, "Prior revision:")
        x, y = slot.baseline
        box = self.text("(superseded)", x, y, size=10.0, gray=0.4, record=False)
        # Remember which op draws the placeholder, so the earlier revision can be built
        # by substituting that one operator. Byte-level replacement of the serialized
        # stream is not safe: the placeholder's own parentheses get escaped.
        self._prior_revision = (value.text, self._tj_index)
        self._record_planted(probe_id, value.text)
        probe = Probe(
            id=probe_id, kind=ProbeKind.TEXT_SPAN, must_redact=value.must_redact,
            severity=value.severity, category=value.category, value=value.text,
            units=value.units,
            ambiguous=value.ambiguous,
            rationale=value.rationale,
            bbox=BBox(0, *box), channel=Channel.TEXT_LAYER,
            difficulty=Difficulty.HARD, trap="prior_revision",
        )
        self._probes.append(probe)
        return probe

    def note(self, slot: Slot, text: str, *, size: float = 5.4, gray: float = 0.42,
             leading: float = 1.3) -> float:
        """Draw `text` wrapped to the slot width; return the height it used.

        Wrapping is exact rather than estimated because the face is metrically fixed -
        one of the reasons this generator uses a monospaced font at all.
        """
        self._register(text)
        per_line = max(20, int(slot.width / (COURIER.char_width * size)))
        lines = textwrap.wrap(text, per_line) or [""]
        y = slot.y + slot.height - size
        for line in lines:
            self.text(line, slot.x, y, size=size, gray=gray)
            y -= size * leading
        return len(lines) * size * leading

    def section_title(self, slot: Slot, title: str) -> None:
        """A small heading with a hairline under it, to separate one group from the next."""
        self._register(title)
        self.text(title, slot.x, slot.y + 3.0, font=COURIER_BOLD, size=6.2, gray=0.34)
        self.rect(slot.x, slot.y + 0.6, slot.width, 0.4, gray=0.72)

    def boilerplate(self, content: str, slot: Slot, *, size: float = 8.0,
                    gray: float = 0.25) -> None:
        """Non-probe page text. Registered so probe values cannot collide with it."""
        self._register(content)
        self.text(content, slot.x, slot.y + slot.height * 0.5, size=size, gray=gray)

    # --- emit -----------------------------------------------------------------------

    def _compose(self, ops: Sequence[str]) -> bytes:
        head = ["q"]
        for f in self.layout.fiducials():
            half = f.size / 2
            head.append(f"0 g {_fmt(f.x - half)} {_fmt(f.y - half)} "
                        f"{_fmt(f.size)} {_fmt(f.size)} re f")
            if f.kind == "donut":
                inner = f.size / 2
                head.append(f"1 g {_fmt(f.x - inner / 2)} {_fmt(f.y - inner / 2)} "
                            f"{_fmt(inner)} {_fmt(inner)} re f")
        head.append("Q")
        return ("\n".join(head) + "\n" + "\n".join(ops)).encode("latin-1")

    def _xmp(self) -> bytes:
        rows = "".join(
            f"<pdfx:{k}>{v}</pdfx:{k}>" for k, v in self._xmp_extra
        )
        return (
            '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" '
            'xmlns:pdfx="http://ns.adobe.com/pdfx/1.3/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{self.case_id}</dc:title>"
            f"<dc:source>{REPO_URL}</dc:source>"
            f"<dc:publisher>{PROJECT_URL}</dc:publisher>"
            f"<pdfx:GeneratedAt>{self.generated_at}</pdfx:GeneratedAt>"
            f"{rows}"
            "</rdf:Description></rdf:RDF></x:xmpmeta>"
            '<?xpacket end="w"?>'
        ).encode()

    def check_graphics_state(self, stream: bytes) -> list[str]:
        """No colour operator may take effect outside a q/Q pair.

        A stray colour survives into every later operator that does not set its own. The
        symptom is content that is present in the text layer and invisible on the page -
        which byte-level checks cannot see, and which would silently make the coverage
        and OCR checks score a blank region.
        """
        problems: list[str] = []
        depth = 0
        for n, line in enumerate(stream.decode("latin-1").splitlines(), 1):
            tokens = line.split()
            for token in tokens:
                if token == "q":
                    depth += 1
                elif token == "Q":
                    depth -= 1
                    if depth < 0:
                        problems.append(f"line {n}: Q without matching q")
                        depth = 0
                elif token in ("g", "rg", "k", "cs", "sc", "scn") and depth == 0:
                    problems.append(f"line {n}: colour operator {token!r} outside q/Q")
        if depth != 0:
            problems.append(f"{depth} unclosed q at end of stream")
        return problems

    def check_spatial_isolation(self, *, pad: float = 1.0) -> list[str]:
        """No two probe boxes may overlap, and no probe box may reach into a caption.

        docs/datasets/core.md requires it: if one redaction covers parts of two probes,
        neither outcome is attributable. Rotated and stacked runs are the usual culprit,
        because they extend far outside the slot a horizontal run would occupy.
        """
        problems: list[str] = []
        boxed = [(p.id, p.bbox) for p in self._probes if p.bbox is not None]
        for i, (a_id, a) in enumerate(boxed):
            for b_id, b in boxed[i + 1 :]:
                if (a.x0 - pad < b.x1 and b.x0 - pad < a.x1
                        and a.y0 - pad < b.y1 and b.y0 - pad < a.y1):
                    problems.append(f"{a_id} and {b_id} overlap")
        # Boxes are font metrics, not ink: a caption's descender box grazing the value's
        # ascender box by a fraction of a point touches no pixel. More than `pad` does.
        for probe_id, box in boxed:
            for owner, (x0, y0, x1, y1) in self._captions:
                if (min(box.x1, x1) - max(box.x0, x0) > pad
                        and min(box.y1, y1) - max(box.y0, y0) > pad):
                    problems.append(f"{probe_id} reaches into the caption of {owner}")
        return problems

    def validate(self) -> list[str]:
        """Invariants the scorer depends on. Non-empty means do not emit."""
        problems: list[str] = self.check_spatial_isolation()
        # A unit belongs to one *value*. The same value planted again under another
        # rendering condition - one subject, many cells - is the sanctioned repeat;
        # a unit reaching across two different values is the defect this catches.
        owners: dict[str, tuple[str, str]] = {}
        for probe in self._probes:
            for unit in probe.units:
                folded = unit.casefold()
                if len(folded) < 4:
                    problems.append(f"{probe.id}: unit {unit!r} shorter than L_min")
                held = owners.get(folded)
                if held is not None and held[0] != (probe.value or ""):
                    problems.append(
                        f"unit {unit!r} shared by {held[1]} and {probe.id} "
                        f"({held[0]!r} vs {probe.value!r})"
                    )
                elif held is None:
                    owners[folded] = (probe.value or "", probe.id)
        # No unit may survive the scorer's own leak test against the rest of the page.
        # The test has to be *that* test, not containment: a unit sharing a long enough
        # run with a label or a heading makes a correct redaction score as a leak, and
        # the generator is the only place that can prevent it.
        for probe in self._probes:
            if not probe.units:
                continue
            others = " ".join(t for t in self._rendered if t != probe.value)
            found = disclosure(probe.units, others)
            if found.leaked:
                problems.append(
                    f"{probe.id}: unit {found.unit!r} shares {found.matched!r} with "
                    f"other text on the page, enough for the leak test to match"
                )
        if not self._probes:
            problems.append("case has no probes")
        return problems

    def build_pdf(self) -> bytes:
        w = PdfWriter()
        font_refs = {f.resource: w.add(f.dictionary()) for f in FONTS}
        content_ref = w.reserve()
        page_ref, pages_ref = w.reserve(), w.reserve()

        resources: dict[str, Any] = {"Font": dict(font_refs)}
        if self._images:
            resources["XObject"] = {
                name: raster.add_to(w) for name, raster in self._images
            }
        page: dict[str, Any] = {
            "Type": Name("Page"), "Parent": pages_ref,
            "MediaBox": [0, 0, self.layout.width, self.layout.height],
            "Contents": content_ref, "Resources": resources,
        }
        if self._annots:
            page["Annots"] = [w.add(a) for a in self._annots]
        w.put(page_ref, page)
        w.put(pages_ref, {"Type": Name("Pages"), "Kids": [page_ref], "Count": 1})

        catalog: dict[str, Any] = {"Type": Name("Catalog"), "Pages": pages_ref}
        if self._ocgs:
            ocg_refs = [
                w.add({"Type": Name("OCG"), "Name": label}) for _, label in self._ocgs
            ]
            resources["Properties"] = {
                res_name: ref
                for (res_name, _), ref in zip(self._ocgs, ocg_refs, strict=True)
            }
            catalog["OCProperties"] = {
                "OCGs": ocg_refs,
                "D": {"OFF": ocg_refs, "Order": ocg_refs},  # hidden by default
            }
        catalog_ref = w.add(catalog)

        # The seed is deliberately absent. This file is handed to the vendor under
        # measurement, and the seed regenerates the case - including a holdout case.
        # It lives in ground_truth.json, which stays with us.
        info: dict[str, Any] = {
            "Title": self.case_id,
            "Producer": f"pdfredeval {GENERATOR_VERSION} ({PROJECT_URL})",
            "Creator": f"pdfredeval {GENERATOR_VERSION}",
            "Subject": f"Redaction benchmark case - {REPO_URL}",
            "CreationDate": pdf_date(self.generated_at or ""),
            "ModDate": pdf_date(self.generated_at or ""),
        }
        info.update(self._info)
        info_ref = w.add(info)
        meta_ref = w.add_stream(
            {"Type": Name("Metadata"), "Subtype": Name("XML")}, self._xmp()
        )
        catalog["Metadata"] = meta_ref

        current = self._compose(self._ops)
        if self._prior_revision is not None:
            # The earlier revision carries the secret; the current one supersedes it.
            secret_text, tj_index = self._prior_revision
            ops = list(self._ops)
            ops[tj_index] = escape_show_string(secret_text).decode("latin-1") + " Tj"
            w.put(content_ref, Stream({}, self._compose(ops)))
            w.trailer = {"Root": catalog_ref, "Info": info_ref}
            return w.build_with_revision({content_ref.num: Stream({}, current)})

        w.put(content_ref, Stream({}, current))
        w.trailer = {"Root": catalog_ref, "Info": info_ref}
        return w.build()

    def ground_truth(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "family": self.family,
            "dataset_revision": self.dataset_revision,
            "generator_version": GENERATOR_VERSION,
            "generated_at": self.generated_at,
            "seed": self.seed,
            "page_size": [self.layout.width, self.layout.height],
            "fiducials": [f.to_dict() for f in self.layout.fiducials()],
            "fonts": dict(sorted(self._fonts_used.items())),
            "probes": [p.to_dict() for p in self._probes],
        }

    def check_planted(self, pdf: bytes) -> list[str]:
        """Every probe value must be findable in the input bytes.

        A probe whose value was never actually written scores as "no leak" forever - a
        silent false pass, and the worst failure this generator can produce. Checked on
        the built bytes, after every encoding and revision trick has been applied.
        """
        missing = []
        for probe in self._probes:
            if not probe.value:
                continue
            patterns = self._planted.get(probe.id)
            if not patterns:
                missing.append(f"{probe.id} (never recorded as planted)")
                continue
            utf16 = probe.value.encode("utf-16-be").hex().upper().encode("ascii")
            for pattern in patterns:
                if pattern not in pdf and utf16 not in pdf:
                    missing.append(
                        f"{probe.id} ({probe.trap or probe.category}): {pattern[:24]!r}"
                    )
                    break
        return missing

    def emit(self, out_dir: Path | str) -> Case:
        """Write `<case_id>.pdf` and `ground_truth.json`, refusing an invalid case."""
        problems = self.validate()
        if problems:
            raise ValueError(
                f"case {self.case_id} violates scorer invariants:\n  "
                + "\n  ".join(problems)
            )
        pdf = self.build_pdf()
        xref_problems = check_xref(pdf)
        if xref_problems:
            raise ValueError(f"case {self.case_id} has a broken xref: {xref_problems}")
        state_problems = self.check_graphics_state(self._compose(self._ops))
        if state_problems:
            raise ValueError(
                f"case {self.case_id} has leaking graphics state: {state_problems}"
            )
        not_planted = self.check_planted(pdf)
        if not_planted:
            raise ValueError(
                f"case {self.case_id}: probe values absent from the PDF, so they could "
                f"never leak: {', '.join(not_planted)}"
            )

        out = Path(out_dir) / self.case_id
        out.mkdir(parents=True, exist_ok=True)
        name = case_pdf_name(self.case_id)
        (out / name).write_bytes(pdf)
        stale = out / LEGACY_CASE_PDF
        if stale.exists():
            stale.unlink()
        truth = {**self.ground_truth(), "pdf": name}
        (out / "ground_truth.json").write_text(json.dumps(truth, indent=2) + "\n")
        return Case.from_dir(out, dataset_revision=self.dataset_revision)
