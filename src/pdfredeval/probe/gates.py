"""Document survivability: is the redacted file still a usable document?

A redacted file nobody can open, or one flattened to a picture, is a failure of a
different kind - and it is a failure that *flatters* every privacy metric. Rasterise the
page and `LR` goes to zero while the document is destroyed, which is why
docs/metrics/redaction.md insists these are published as flags beside the scores and
never folded into them.

Binary, cheap, run on every output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..textmatch import normalize
from ..types import Case
from .text import UnreadablePdf, page_text, reader


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Survivability:
    gates: tuple[Gate, ...]
    text_retention: float
    output_chars: int
    input_chars: int

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.gates)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(g.name for g in self.gates if not g.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "text_retention": round(self.text_retention, 4),
            "input_chars": self.input_chars,
            "output_chars": self.output_chars,
            "gates": [g.to_dict() for g in self.gates],
        }


def _page_sizes(document: Any) -> list[tuple[float, float]]:
    sizes = []
    for page in document.pages:
        box = page.mediabox
        sizes.append((float(box.width), float(box.height)))
    return sizes


def text_retention(case_text: str, output_text: str, targets: tuple[str, ...]) -> float:
    """`TR`: of the characters that should have remained, how many still extract?

    The denominator is the input's text with every target value taken out - a tool is
    not penalised for removing what it was asked to remove. Matching is by longest
    common subsequence over the normalised forms, so reflowed whitespace and reordered
    blocks do not read as loss.
    """
    from difflib import SequenceMatcher

    reference = normalize(case_text)
    for value in targets:
        folded = normalize(value)
        if folded:
            reference = reference.replace(folded, " ")
    reference = " ".join(reference.split())
    if not reference:
        return 1.0

    matcher = SequenceMatcher(None, reference, normalize(output_text), autojunk=False)
    kept = sum(block.size for block in matcher.get_matching_blocks())
    return min(1.0, kept / len(reference))


def check(
    case: Case,
    output_pdf: bytes,
    case_text: str,
    output_text: str,
    *,
    min_text_retention: float = 0.9,
    min_char_survival: float = 0.05,
    geometry_tolerance: float = 0.01,
) -> Survivability:
    """Run every gate. A gate that cannot be evaluated fails: silence is not a pass."""
    gates: list[Gate] = []
    input_chars = len(normalize(case_text))
    output_chars = len(normalize(output_text))

    try:
        document = reader(output_pdf)
        gates.append(Gate("opens", True))
    except UnreadablePdf as exc:
        return Survivability(
            gates=(
                Gate("opens", False, str(exc)),
                Gate("pages", False, "output does not parse"),
                Gate("geometry", False, "output does not parse"),
                Gate("text_retained", False, "output does not parse"),
                Gate("not_rasterised", False, "output does not parse"),
            ),
            text_retention=0.0, output_chars=0, input_chars=input_chars,
        )

    sizes = _page_sizes(document)
    gates.append(Gate(
        "pages", len(sizes) == 1,
        f"{len(sizes)} page(s); the case is a single page",
    ))

    if case.page_size and sizes:
        want_w, want_h = case.page_size
        got_w, got_h = sizes[0]
        # A quarter-turned page is the same page. Accept either orientation and let the
        # aligner report the rotation; a rotated output is not a geometry failure.
        upright = (abs(got_w - want_w) / want_w <= geometry_tolerance
                   and abs(got_h - want_h) / want_h <= geometry_tolerance)
        turned = (abs(got_h - want_w) / want_w <= geometry_tolerance
                  and abs(got_w - want_h) / want_h <= geometry_tolerance)
        gates.append(Gate(
            "geometry", upright or turned,
            f"{got_w:.1f}x{got_h:.1f}pt against {want_w:.1f}x{want_h:.1f}pt",
        ))
    else:  # pragma: no cover - ground truth always records a page size
        gates.append(Gate("geometry", False, "ground truth records no page size"))

    retention = text_retention(
        case_text, output_text,
        tuple(p.value for p in case.targets if p.value),
    )
    gates.append(Gate(
        "text_retained", retention >= min_text_retention,
        f"TR = {retention:.3f}, floor {min_text_retention}",
    ))

    survival = output_chars / input_chars if input_chars else 1.0
    gates.append(Gate(
        "not_rasterised", survival > min_char_survival,
        f"{output_chars} of {input_chars} extractable characters survive "
        f"({survival:.1%}); flattening the page to an image redacts everything "
        f"perfectly and destroys the document",
    ))
    return Survivability(tuple(gates), retention, output_chars, input_chars)


def case_text(case: Case) -> str:
    """The input's own extractable text, the denominator for retention."""
    try:
        plain, hidden = page_text(reader(case.pdf_bytes))
    except UnreadablePdf:  # pragma: no cover - our own generated file
        return ""
    return plain + hidden
