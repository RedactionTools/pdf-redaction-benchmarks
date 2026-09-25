"""Observed mode: what the tool says it found, matched against what is there.

Detection is only *directly* measurable when the tool tells us what it found. Most tools
are UI-only and never do, and in that case docs/metrics/detection.md is blunt about the
consequence: report redaction outcomes and say plainly that detection is unidentifiable.
Do not publish a "detection recall" that is really a leak rate wearing a different label.

So this runs only when a run supplies `entities.json`, and its absence is reported as
inferred mode rather than as a zero.

Precision *is* legitimate here, unlike in probe outcomes: its denominator is the tool's
own output, not a target/distractor mix we chose. And IoU is right here for the same
reason it is wrong for coverage - we are judging a box the tool chose to report, so
overshoot is a real error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..textmatch import normalize
from ..types import BBox, Case, Probe

ENTITIES_NAME = "entities.json"


@dataclass(frozen=True, slots=True)
class Entity:
    """One entity a tool reported finding."""

    category: str | None = None
    text: str | None = None
    span: tuple[int, int] | None = None
    bbox: BBox | None = None
    score: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Entity:
        span = d.get("span")
        box = d.get("bbox")
        return cls(
            category=d.get("category") or d.get("type") or d.get("label"),
            text=d.get("text") or d.get("value"),
            span=(int(span[0]), int(span[1])) if span else None,
            bbox=BBox.from_dict(box) if box else None,
            score=d.get("score"),
        )


@dataclass(frozen=True, slots=True)
class Match:
    entity: Entity
    probe: Probe
    overlap: float
    typed: bool


@dataclass(frozen=True, slots=True)
class DetectionReport:
    mode: str  # "observed" | "inferred"
    note: str = ""
    recall: float | None = None
    precision: float | None = None
    f1: float | None = None
    type_accuracy: float | None = None
    matched: int = 0
    targets: int = 0
    reported: int = 0
    confusion: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "note": self.note,
            "recall": self.recall,
            "precision": self.precision,
            "f1": self.f1,
            "type_accuracy": self.type_accuracy,
            "matched": self.matched,
            "targets": self.targets,
            "reported": self.reported,
            "confusion": self.confusion,
        }


INFERRED = DetectionReport(
    mode="inferred",
    note=(
        "the tool reported no entity list, so detection is unidentifiable: a detection "
        "miss and a redaction failure are the same observation. Redaction outcomes are "
        "reported instead."
    ),
)


def load_entities(run_dir: Path | str) -> list[Entity] | None:
    """Read `entities.json` from a run directory, if the adapter left one."""
    path = Path(run_dir) / ENTITIES_NAME
    if not path.exists():
        return None
    payload = json.loads(path.read_text())
    items = payload.get("entities", payload) if isinstance(payload, dict) else payload
    return [Entity.from_dict(item) for item in items]


def iou(a: BBox, b: BBox) -> float:
    lo_x, hi_x = max(a.x0, b.x0), min(a.x1, b.x1)
    lo_y, hi_y = max(a.y0, b.y0), min(a.y1, b.y1)
    if hi_x <= lo_x or hi_y <= lo_y:
        return 0.0
    intersection = (hi_x - lo_x) * (hi_y - lo_y)
    return intersection / (a.area + b.area - intersection)


def overlap(entity: Entity, probe: Probe) -> float:
    """How well a reported entity lines up with a probe, by whatever both carry.

    A bare `span` is not usable on its own: character offsets are into *the tool's own*
    extraction, and we do not have that text, so there is no common origin to measure
    against. An entity that reports a span is expected to report its text too, and the
    text is what gets matched.
    """
    if entity.bbox and probe.bbox:
        return iou(entity.bbox, probe.bbox)
    if entity.text and probe.value:
        return _text_overlap(normalize(entity.text), normalize(probe.value))
    return 0.0


def _text_overlap(a: str, b: str) -> float:
    """Jaccard over the characters two strings share as a contiguous run."""
    from ..textmatch import lcs_length

    if not a or not b:
        return 0.0
    shared = lcs_length(a, b)
    union = len(a) + len(b) - shared
    return shared / union if union else 0.0


def match(
    entities: list[Entity], case: Case, *, tau_span: float = 0.5, tau_iou: float = 0.5
) -> list[Match]:
    """Greedy one-to-one assignment, highest overlap first."""
    candidates: list[tuple[float, int, int]] = []
    probes = list(case.probes)
    for i, entity in enumerate(entities):
        for j, probe in enumerate(probes):
            score = overlap(entity, probe)
            threshold = tau_iou if (entity.bbox and probe.bbox) else tau_span
            if score >= threshold:
                candidates.append((score, i, j))

    candidates.sort(key=lambda t: -t[0])
    used_entities: set[int] = set()
    used_probes: set[int] = set()
    matches: list[Match] = []
    for score, i, j in candidates:
        if i in used_entities or j in used_probes:
            continue
        used_entities.add(i)
        used_probes.add(j)
        entity, probe = entities[i], probes[j]
        matches.append(Match(
            entity=entity, probe=probe, overlap=score,
            typed=bool(entity.category and probe.category
                       and entity.category.upper() == probe.category.upper()),
        ))
    return matches


def report(
    entities: list[Entity] | None,
    case: Case,
    *,
    tau_span: float = 0.5,
    tau_iou: float = 0.5,
) -> DetectionReport:
    """Detection recall, precision, F1, type accuracy and the confusion matrix."""
    if entities is None:
        return INFERRED

    matches = match(entities, case, tau_span=tau_span, tau_iou=tau_iou)
    targets = case.targets
    matched_targets = [m for m in matches if m.probe.must_redact and not m.probe.ambiguous]

    recall = len(matched_targets) / len(targets) if targets else None
    precision = len(matches) / len(entities) if entities else None
    f1 = (
        2 * recall * precision / (recall + precision)
        if recall and precision and (recall + precision)
        else (0.0 if recall is not None and precision is not None else None)
    )
    typed = sum(1 for m in matches if m.typed)
    type_accuracy = typed / len(matches) if matches else None

    confusion: dict[str, dict[str, int]] = {}
    for m in matches:
        truth = (m.probe.category or "?").upper()
        said = (m.entity.category or "?").upper()
        confusion.setdefault(truth, {}).setdefault(said, 0)
        confusion[truth][said] += 1

    return DetectionReport(
        mode="observed",
        note="",
        recall=round(recall, 6) if recall is not None else None,
        precision=round(precision, 6) if precision is not None else None,
        f1=round(f1, 6) if f1 is not None else None,
        type_accuracy=round(type_accuracy, 6) if type_accuracy is not None else None,
        matched=len(matches),
        targets=len(targets),
        reported=len(entities),
        confusion=confusion,
    )
