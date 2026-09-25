"""Every constant a score depends on, in one object that is published with it.

docs/metrics/core.md: "Every published score names the dataset revision **and** this
table. Change a threshold and you have changed the benchmark." So the table is data, not
scattered literals - `Thresholds.to_dict()` goes into every report, and a result computed
with a modified table says so on its face.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

#: `D_c`, the shortest run of a category's own characters that still discloses.
#:
#: docs/metrics/core.md: a ratio alone is wrong for structured identifiers. Masking
#: `123-45-6789` to `XXX-XX-6789` leaves RDR = 0.36 - a pass on the proportional test,
#: and a disclosed national id. Categories absent from this map get the proportional
#: test only, which is right for names and free text: no short prefix of a name means
#: anything on its own.
DISCLOSURE_LENGTH: dict[str, int] = {
    "NATIONAL_ID": 4,
    "CARD": 4,
    "PASSPORT": 4,
    "PHONE": 4,
    "IBAN": 4,
    "ACCOUNT": 4,
    "EMAIL": 4,
    "POSTCODE": 4,
    "PLATE": 4,
}


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Defaults from docs/metrics/core.md, plus the gate limits of redaction.md."""

    # --- the published defaults table ---------------------------------------------
    dpi: int = 300
    delta_px: int = 24
    tau_text: float = 0.5
    l_min: int = 4
    tau_cov: float = 0.98
    tau_hash: int = 10
    tau_span: float = 0.5
    tau_iou: float = 0.5
    z: float = 1.96
    beta: float = 2.0
    disclosure_length: Mapping[str, int] = field(default_factory=lambda: DISCLOSURE_LENGTH)

    # --- survivability gates (docs/metrics/redaction.md) ---------------------------
    min_text_retention: float = 0.9
    min_char_survival: float = 0.05
    geometry_tolerance: float = 0.01

    # --- implementation constants the metrics do not name --------------------------
    #: Neighbourhood for `Spill` and `Collateral`, in points. One line height.
    spill_epsilon_pt: float = 12.0
    #: Morphological open/close radius on the change mask, in pixels at `dpi`.
    morph_radius: int = 1
    #: Fraction of a probe box that must carry ink in the *input* before the rendered
    #: pixel layer has anything to say about it. Invisible text and annotation values
    #: draw nothing, so coverage against an empty box would report a leak forever.
    min_ink_fraction: float = 0.01
    #: Reprojection error above which alignment is not trusted and every raster layer
    #: reports `unavailable` rather than a verdict.
    max_residual_px: float = 6.0
    #: Fraction of the page in the change mask above which the output is treated as
    #: re-rendered wholesale, and pixel verdicts are flagged rather than believed.
    page_rewritten_fraction: float = 0.5
    #: Slack round a probe box when deciding whether a piece of located text is drawn
    #: in it, in points. Glyph boxes and OCR word boxes overhang a tight ground-truth
    #: box by a stroke or two; a neighbouring probe is further away than this.
    locate_margin_pt: float = 2.0
    #: The pixel verdict, in characters: a text probe leaks through pixels when at least
    #: this many characters' worth of its ink is still showing. Coverage is reported
    #: beside it; this is what decides.
    glyph_legible: float = 0.5
    #: The same verdict for a probe with no characters to count (an image): the share of
    #: its ink that may still show.
    tau_reveal: float = 0.02
    #: Neighbourhood a pixel is compared against to decide it is ink, in points. Wider
    #: than a stroke, narrower than a pattern in the ground.
    glyph_window_pt: float = 2.0
    #: Revealed fragments this close together are one residue, in points.
    glyph_merge_pt: float = 0.5
    #: A residue smaller than this share of one character's ink is a speck, not text.
    glyph_floor: float = 0.25

    def d_c(self, category: str | None, declared: int | None = None) -> int | None:
        """The absolute disclosure length for a category.

        A value declared in the ground truth wins: docs/datasets/core.md lists `D_c` as
        a per-probe field, so a dataset may know better than this table.
        """
        if declared is not None:
            return declared
        return self.disclosure_length.get(category or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dpi": self.dpi,
            "delta_px": self.delta_px,
            "tau_text": self.tau_text,
            "l_min": self.l_min,
            "tau_cov": self.tau_cov,
            "tau_hash": self.tau_hash,
            "tau_span": self.tau_span,
            "tau_iou": self.tau_iou,
            "z": self.z,
            "beta": self.beta,
            "disclosure_length": dict(sorted(self.disclosure_length.items())),
            "min_text_retention": self.min_text_retention,
            "min_char_survival": self.min_char_survival,
            "geometry_tolerance": self.geometry_tolerance,
            "spill_epsilon_pt": self.spill_epsilon_pt,
            "morph_radius": self.morph_radius,
            "min_ink_fraction": self.min_ink_fraction,
            "max_residual_px": self.max_residual_px,
            "page_rewritten_fraction": self.page_rewritten_fraction,
            "locate_margin_pt": self.locate_margin_pt,
            "glyph_legible": self.glyph_legible,
            "tau_reveal": self.tau_reveal,
            "glyph_window_pt": self.glyph_window_pt,
            "glyph_merge_pt": self.glyph_merge_pt,
            "glyph_floor": self.glyph_floor,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Thresholds:
        known = {f for f in cls.__slots__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def but(self, **changes: Any) -> Thresholds:
        """A copy with overrides, for a sensitivity sweep."""
        return replace(self, **changes)


DEFAULTS = Thresholds()
