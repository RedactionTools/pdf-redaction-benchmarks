"""Per-layer removal tests: does this probe survive on this surface?

One rule per layer, each answering in three states. The third state is the point:

* **leaked** - the value is still there.
* **clean** - the layer was read and the value is gone.
* **not applicable** - the layer has nothing to say about this probe. Invisible text
  cannot leak through pixels; a value with no recorded image hash cannot be matched
  against the output's images.
* **unavailable** - the layer could not be read at all.

Collapsing the last two into "clean" is how a scorer reports a passing grade for a check
that never ran, and `docs/metrics/redaction.md` singles that out as the failure mode of
the OCR layer in particular.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..probe import Observations
from ..probe.base import TEXT_LAYERS, Layer, Reading
from ..probe.mask import ink
from ..textmatch import disclosure, normalize
from ..thresholds import Thresholds
from ..types import Probe, ProbeKind

#: Probe kinds whose removal is judged by image identity rather than by text.
IMAGE_KINDS = frozenset({
    ProbeKind.IMAGE, ProbeKind.FACE, ProbeKind.SIGNATURE, ProbeKind.STAMP,
    ProbeKind.CODE,
})

#: A ground-truth value that *is* a 64-bit perceptual hash, for image probes.
_PHASH = re.compile(r"\A[0-9a-fA-F]{16}\Z")

#: The pixel layer describes a box, and these probes' boxes do not describe their value.
#: A prior-revision probe's box is the placeholder drawn over the secret, not the secret.
_NO_PIXEL_TRAP = frozenset({"prior_revision"})


class Verdict(str, Enum):
    LEAKED = "leaked"
    CLEAN = "clean"
    NOT_APPLICABLE = "not_applicable"
    UNAVAILABLE = "unavailable"

    @property
    def decided(self) -> bool:
        """Did this layer actually produce evidence either way?"""
        return self in (Verdict.LEAKED, Verdict.CLEAN)


@dataclass(frozen=True, slots=True)
class LayerResult:
    layer: Layer
    verdict: Verdict
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def leaked(self) -> bool:
        return self.verdict is Verdict.LEAKED

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"layer": self.layer.value, "verdict": self.verdict.value}
        if self.reason:
            out["reason"] = self.reason
        if self.evidence:
            out["evidence"] = self.evidence
        return out


# --- the individual tests ------------------------------------------------------------


def text_rule(
    probe: Probe, layer: Layer, observations: Observations, thresholds: Thresholds
) -> LayerResult:
    """Residual disclosure of the probe's units in one text-bearing layer."""
    reading = observations.readings.get(layer)
    if reading is None:
        return LayerResult(layer, Verdict.UNAVAILABLE, "layer not probed")
    if not reading.readable:
        return LayerResult(layer, Verdict.UNAVAILABLE, reading.unavailable or "")
    if not probe.units:
        return LayerResult(layer, Verdict.NOT_APPLICABLE, "probe carries no text value")

    haystack, scope = _haystack(probe, reading)
    found = disclosure(
        probe.units, haystack,
        tau_text=thresholds.tau_text,
        l_min=thresholds.l_min,
        d_c=thresholds.d_c(probe.category, probe.disclosure_length),
    )
    if not found.leaked:
        return LayerResult(layer, Verdict.CLEAN,
                           evidence={"rdr": round(found.rdr, 4), "scope": scope})
    return LayerResult(layer, Verdict.LEAKED, evidence={
        "scope": scope,
        "rdr": round(found.rdr, 4),
        "unit": found.unit,
        "matched": found.matched,
        "test": found.test,
        "grade": found.grade,
    })


def _haystack(probe: Probe, reading: Reading) -> tuple[str, str]:
    """The text a probe is judged against, and whether it was narrowed to the probe.

    A located layer is read *at the probe*: its own box, plus any text drawn outside
    every probe box - a tool that moves text rather than deleting it still leaks it.
    What is left out is text inside another probe's box. That text is evidence about the
    other probe, and a page that repeats one value under forty conditions would
    otherwise turn a single surviving copy into forty leaks.
    """
    if reading.spans is None or probe.bbox is None or probe.bbox.page != 0:
        return reading.text, "page"
    return ("".join(span.text for span in reading.spans
                    if not span.probes or probe.id in span.probes), "probe")


def pixel_rule(
    probe: Probe, observations: Observations, thresholds: Thresholds
) -> LayerResult:
    """`Cov_i >= tau_cov` against the change mask - coverage, not IoU."""
    layer = Layer.RENDERED_PIXELS
    if observations.mask is None:
        return LayerResult(layer, Verdict.UNAVAILABLE,
                           observations.why_unavailable(layer) or "no change mask")
    if probe.bbox is None:
        return LayerResult(layer, Verdict.NOT_APPLICABLE, "probe has no box")
    if probe.trap in _NO_PIXEL_TRAP:
        return LayerResult(layer, Verdict.NOT_APPLICABLE,
                           f"a {probe.trap} probe's box holds the placeholder, not the value")

    visible = (
        ink(observations.source_render, probe.bbox, observations.frame,
            dark=255 - thresholds.delta_px)
        if observations.source_render is not None else 1.0
    )
    if visible < thresholds.min_ink_fraction:
        return LayerResult(
            layer, Verdict.NOT_APPLICABLE,
            "nothing is drawn at this box in the input, so no pixel of it can leak",
            evidence={"ink": round(visible, 4)},
        )

    coverage = observations.mask.coverage(probe.bbox)
    spill = observations.mask.spill(probe.bbox, thresholds.spill_epsilon_pt)
    evidence: dict[str, Any] = {"coverage": round(coverage, 4), "spill": round(spill, 4),
                                "ink": round(visible, 4)}

    per_pt = observations.frame.dpi / 72.0
    window = int(round(thresholds.glyph_window_pt * per_pt))
    # One character's ink, measured off the input box itself so it scales with type
    # size and weight. A probe with no text value has no characters to count.
    chars = (0 if probe.kind in IMAGE_KINDS
             else len("".join((probe.value or "").split())))
    ink_only = observations.mask.revealed(
        probe.bbox, delta_px=thresholds.delta_px, window_px=window, merge_px=0,
        floor_px=float("inf"),
    )
    if ink_only is None:
        # No renders kept, so only the change mask can speak: the coverage test.
        leaked = coverage < thresholds.tau_cov
        return LayerResult(layer, Verdict.LEAKED if leaked else Verdict.CLEAN,
                           evidence=evidence)
    if ink_only.ink_px == 0:
        return LayerResult(
            layer, Verdict.NOT_APPLICABLE,
            "no glyph stands out at this box in the input, so none can be left showing",
            evidence=evidence,
        )

    per_char = ink_only.ink_px / chars if chars else 0.0
    glyphs = observations.mask.revealed(
        probe.bbox, delta_px=thresholds.delta_px, window_px=window,
        merge_px=max(1, int(round(thresholds.glyph_merge_pt * per_pt))),
        floor_px=thresholds.glyph_floor * per_char,
    )
    assert glyphs is not None  # the renders were there a moment ago
    evidence["revealed"] = round(glyphs.revealed, 4)
    if chars:
        showing = glyphs.revealed_px / per_char
        evidence["revealed_chars"] = round(showing, 2)
        leaked = showing >= thresholds.glyph_legible
    else:
        leaked = glyphs.revealed > thresholds.tau_reveal
    return LayerResult(layer, Verdict.LEAKED if leaked else Verdict.CLEAN,
                       evidence=evidence)


def image_rule(
    probe: Probe, observations: Observations, thresholds: Thresholds
) -> LayerResult:
    """Image identity: a crop is a view, not a deletion."""
    layer = Layer.IMAGE_XOBJECT
    if probe.kind not in IMAGE_KINDS:
        return LayerResult(layer, Verdict.NOT_APPLICABLE, "not an image probe")
    reading = observations.readings.get(layer)
    if reading is not None and not reading.readable:
        return LayerResult(layer, Verdict.UNAVAILABLE, reading.unavailable or "")

    if not (probe.value and _PHASH.match(probe.value)):
        # The generator plants no image probes yet and records no reference hash, so
        # there is nothing to compare an output image against. Said plainly rather than
        # scored as a pass.
        return LayerResult(
            layer, Verdict.NOT_APPLICABLE,
            "ground truth records no perceptual hash for this probe",
        )

    reference = int(probe.value, 16)
    distances = [image.distance(reference) for image in observations.output_images]
    nearest = min(distances, default=64)
    evidence = {"nearest_hamming": nearest, "images": len(distances)}
    if nearest <= thresholds.tau_hash:
        return LayerResult(layer, Verdict.LEAKED, evidence=evidence)
    return LayerResult(layer, Verdict.CLEAN, evidence=evidence)


def metadata_rule(
    probe: Probe, observations: Observations, thresholds: Thresholds
) -> LayerResult:
    """The same disclosure test, over every metadata value in the file."""
    return text_rule(probe, Layer.METADATA, observations, thresholds)


def font_subset_rule(
    probe: Probe, observations: Observations, thresholds: Thresholds
) -> LayerResult:
    """Glyphs an embedded subset can still draw that no visible run uses.

    Set containment, not a substring search: a subset is an alphabet, not a string, and
    what it discloses is which characters the removed text needed. That narrows a value
    without revealing it, which is why the layer is rated low.
    """
    layer = Layer.FONT_SUBSET
    reading = observations.readings.get(layer)
    if reading is None or not reading.readable:
        return LayerResult(layer, Verdict.UNAVAILABLE,
                           (reading.unavailable if reading else "layer not probed") or "")
    residue = set(reading.text)
    if not residue:
        return LayerResult(layer, Verdict.NOT_APPLICABLE,
                           "no embedded font subset carries unused glyphs")
    if not probe.value:
        return LayerResult(layer, Verdict.NOT_APPLICABLE, "probe carries no text value")

    needed = set(normalize(probe.value)) - {" "}
    if len(needed) < thresholds.l_min:
        # The alphabet of a short value narrows nothing: half the page shares it.
        return LayerResult(layer, Verdict.NOT_APPLICABLE,
                           "the value's alphabet is too small to narrow anything")
    if needed and needed <= residue:
        return LayerResult(layer, Verdict.LEAKED, evidence={
            "alphabet": "".join(sorted(needed)), "residue": len(residue),
        })
    return LayerResult(layer, Verdict.CLEAN, evidence={"residue": len(residue)})


# --- the combination -----------------------------------------------------------------


def evaluate(
    probe: Probe, observations: Observations, thresholds: Thresholds
) -> dict[Layer, LayerResult]:
    """Every layer's verdict on one probe.

    `leak_i = OR over layers`, so the worst layer sets the outcome: a probe counts as
    redacted only if it survives **none** of them.
    """
    results: dict[Layer, LayerResult] = {}
    for layer in TEXT_LAYERS:
        results[layer] = text_rule(probe, layer, observations, thresholds)
    results[Layer.METADATA] = metadata_rule(probe, observations, thresholds)
    results[Layer.RENDERED_PIXELS] = pixel_rule(probe, observations, thresholds)
    results[Layer.IMAGE_XOBJECT] = image_rule(probe, observations, thresholds)
    results[Layer.FONT_SUBSET] = font_subset_rule(probe, observations, thresholds)
    return results
