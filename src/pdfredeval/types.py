"""Core value types: probes, cases, and the enums the contract speaks in.

Mirrors `docs/datasets/core.md` (ground truth) and `docs/metrics/core.md` (severity
weights). Everything here is frozen and JSON round-trippable: ground truth is data, and
the manual path means it has to survive a trip through disk and back days later.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Stage(str, Enum):
    EXTRACTION = "extraction"
    DETECTION = "detection"
    REDACTION = "redaction"


class Surface(str, Enum):
    """The product surface. `vendor:surface` ids exist because these differ."""

    WEB = "web"
    API = "api"
    DESKTOP = "desktop"


class Transport(str, Enum):
    MANUAL = "manual"
    API = "api"


class Mode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"
    BATCH = "batch"


class Status(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    REJECTED = "rejected"

    @property
    def terminal(self) -> bool:
        return self is not Status.PENDING


class Severity(str, Enum):
    """Weights double per level: docs/metrics/core.md."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def weight(self) -> int:
        return _SEVERITY_WEIGHT[self]


_SEVERITY_WEIGHT = {
    Severity.CRITICAL: 8,
    Severity.HIGH: 4,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
}


class ProbeKind(str, Enum):
    TEXT_SPAN = "text_span"
    IMAGE = "image"
    FACE = "face"
    SIGNATURE = "signature"
    CODE = "code"
    STAMP = "stamp"
    METADATA_KEY = "metadata_key"


class Channel(str, Enum):
    """Which extraction channel reaches this probe. Single-channel by construction."""

    TEXT_LAYER = "text_layer"
    METADATA = "metadata"
    EMBEDDED_IMAGE = "embedded_image"
    IMAGE_TEXT = "image_text"


class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


# --- rendering conditions (docs/metrics/extraction.md) ------------------------------

ORIENTATIONS = ("horizontal", "rot90", "rot270", "rot180", "skew", "vertical")
POLARITIES = ("normal", "inverse", "low-contrast", "on-image", "screened", "highlighted")
PROVENANCES = (
    "vector",
    "print-clean",
    "print-degraded",
    "hand-block",
    "hand-cursive",
    "hand-mixed",
)
SCALES = ("pt10", "pt6", "pt4")


@dataclass(frozen=True, slots=True)
class Conditions:
    """One level per rendering axis. Defaults are the control cell."""

    orientation: str = "horizontal"
    polarity: str = "normal"
    provenance: str = "vector"
    scale: str = "pt10"

    def __post_init__(self) -> None:
        for value, allowed, axis in (
            (self.orientation, ORIENTATIONS, "orientation"),
            (self.polarity, POLARITIES, "polarity"),
            (self.provenance, PROVENANCES, "provenance"),
            (self.scale, SCALES, "scale"),
        ):
            if value not in allowed:
                raise ValueError(f"{axis}={value!r} not one of {allowed}")

    @property
    def is_control(self) -> bool:
        return self == Conditions()

    @property
    def is_handwritten(self) -> bool:
        return self.provenance.startswith("hand-")

    def axes(self) -> dict[str, str]:
        return {
            "orientation": self.orientation,
            "polarity": self.polarity,
            "provenance": self.provenance,
            "scale": self.scale,
        }

    def varied_axes(self) -> dict[str, str]:
        """Axes differing from the control - what a one-factor-at-a-time probe varies."""
        control = Conditions()
        return {k: v for k, v in self.axes().items() if v != getattr(control, k)}

    def to_dict(self) -> dict[str, str]:
        return self.axes()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Conditions:
        return cls(**dict(data or {}))


@dataclass(frozen=True, slots=True)
class BBox:
    """Box in PDF points, origin bottom-left."""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError(f"degenerate bbox: {self}")

    @property
    def area(self) -> float:
        return (self.x1 - self.x0) * (self.y1 - self.y0)

    def to_dict(self) -> dict[str, Any]:
        return {"page": self.page, "x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> BBox:
        return cls(**{k: d[k] for k in ("page", "x0", "y0", "x1", "y1")})


@dataclass(frozen=True, slots=True)
class Fiducial:
    """A registration mark, at the centre given, in PDF points.

    Three identical corner squares plus one distinct marker make the output-to-ground-
    truth mapping recoverable *and* unambiguous under rotation: four identical squares
    would leave a 180-degree flip undetectable, which is exactly what a rescanned page
    produces. The aligner reads these back out of the ground truth.
    """

    role: str  # tl, tr, bl, br
    x: float
    y: float
    size: float = 8.0
    kind: str = "square"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role, "x": round(self.x, 3), "y": round(self.y, 3),
            "size": self.size, "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Fiducial:
        return cls(role=d["role"], x=float(d["x"]), y=float(d["y"]),
                   size=float(d.get("size", 8.0)), kind=d.get("kind", "square"))


@dataclass(frozen=True, slots=True)
class Probe:
    """One independently scored item of ground truth.

    `units` are the disclosure units (docs/metrics/core.md): the value, plus every
    fragment that identifies on its own - a surname, a street, a domain label, the last
    four digits of a card. The generator emits them because it built the value and knows
    which fragments carry identity; scoring must not guess, and a blanket "any run of N
    characters" rule would match a TLD or a coincidental pair of digits.
    """

    id: str
    kind: ProbeKind
    must_redact: bool
    severity: Severity = Severity.HIGH
    category: str | None = None
    value: str | None = None
    units: tuple[str, ...] = ()
    bbox: BBox | None = None
    channel: Channel | None = None
    #: `D_c`: the shortest run of this value's own characters that still discloses.
    #: None defers to the scorer's per-category table (docs/metrics/core.md).
    disclosure_length: int | None = None
    difficulty: Difficulty = Difficulty.EASY
    trap: str | None = None
    conditions: Conditions = field(default_factory=Conditions)
    #: A probe whose correct handling is genuinely arguable - an employer name is
    #: public on an invoice and identifying in a personnel record. Scored apart and
    #: reported as an agreement rate, never folded into the headline rates, because
    #: burying a defensible disagreement inside one number makes the whole benchmark
    #: look arbitrary to a vendor (docs/metrics/detection.md).
    ambiguous: bool = False
    rationale: str | None = None

    def __post_init__(self) -> None:
        if self.kind is ProbeKind.TEXT_SPAN and not self.value:
            raise ValueError(f"probe {self.id}: text_span needs a value")
        if self.value and self.value not in self.units:
            # The whole value is always a disclosure unit.
            object.__setattr__(self, "units", (self.value, *self.units))

    @property
    def weight(self) -> int:
        return self.severity.weight

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "must_redact": self.must_redact,
            "severity": self.severity.value,
            "category": self.category,
            "value": self.value,
            "units": list(self.units),
            "bbox": self.bbox.to_dict() if self.bbox else None,
            "channel": self.channel.value if self.channel else None,
            "disclosure_length": self.disclosure_length,
            "difficulty": self.difficulty.value,
            "trap": self.trap,
            "conditions": self.conditions.to_dict(),
            "ambiguous": self.ambiguous,
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Probe:
        return cls(
            id=d["id"],
            kind=ProbeKind(d["kind"]),
            must_redact=bool(d["must_redact"]),
            severity=Severity(d.get("severity", "high")),
            category=d.get("category"),
            value=d.get("value"),
            units=tuple(d.get("units") or ()),
            bbox=BBox.from_dict(d["bbox"]) if d.get("bbox") else None,
            channel=Channel(d["channel"]) if d.get("channel") else None,
            disclosure_length=d.get("disclosure_length"),
            difficulty=Difficulty(d.get("difficulty", "easy")),
            trap=d.get("trap"),
            conditions=Conditions.from_dict(d.get("conditions")),
            ambiguous=bool(d.get("ambiguous", False)),
            rationale=d.get("rationale"),
        )


@dataclass(frozen=True, slots=True)
class Case:
    """A PDF plus its ground truth, pinned to a dataset revision."""

    case_id: str
    pdf_path: Path
    probes: tuple[Probe, ...] = ()
    dataset_revision: str | None = None
    family: str | None = None
    #: Page size in points, and the registration marks the aligner maps back to.
    page_size: tuple[float, float] | None = None
    fiducials: tuple[Fiducial, ...] = ()

    @property
    def pdf_bytes(self) -> bytes:
        return Path(self.pdf_path).read_bytes()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.pdf_bytes).hexdigest()

    @property
    def targets(self) -> tuple[Probe, ...]:
        return tuple(p for p in self.probes if p.must_redact and not p.ambiguous)

    @property
    def distractors(self) -> tuple[Probe, ...]:
        return tuple(p for p in self.probes if not p.must_redact and not p.ambiguous)

    @property
    def ambiguous(self) -> tuple[Probe, ...]:
        """Probes whose correct handling is arguable; reported, never ranked on."""
        return tuple(p for p in self.probes if p.ambiguous)

    def lexical_conflicts(self) -> dict[str, list[str]]:
        """Disclosure units shared by probes carrying *different* values.

        docs/datasets/core.md requires an identifying token to belong to one value: the
        scorer hunts each unit across the whole output, so a token a distractor shares
        with a target makes a correct redaction score as a leak. The generator must keep
        this empty.

        The one sanctioned repeat is the same value planted again under another
        rendering condition - every carrier of the unit is that subject, so a leak
        attributed through it lands on all of them, which is the honest per-subject
        reading, not on a neighbour it never belonged to.
        """
        carriers: dict[str, dict[str, list[str]]] = {}
        for probe in self.probes:
            for unit in probe.units:
                by_value = carriers.setdefault(unit.casefold(), {})
                by_value.setdefault(probe.value or "", []).append(probe.id)
        return {
            unit: [pid for ids in by_value.values() for pid in ids]
            for unit, by_value in carriers.items()
            if len(by_value) > 1
        }

    @classmethod
    def from_dir(cls, path: Path | str, *, dataset_revision: str | None = None) -> Case:
        """Load `<case_id>/{<case_id>.pdf, ground_truth.json}` (or a legacy `case.pdf`)."""
        from .workspace import case_pdf_path

        path = Path(path)
        truth = json.loads((path / "ground_truth.json").read_text())
        size = truth.get("page_size")
        case_id = truth.get("case_id", path.name)
        return cls(
            case_id=case_id,
            pdf_path=case_pdf_path(path, case_id, truth.get("pdf")),
            probes=tuple(Probe.from_dict(p) for p in truth.get("probes", ())),
            dataset_revision=dataset_revision or truth.get("dataset_revision"),
            family=truth.get("family"),
            page_size=(float(size[0]), float(size[1])) if size else None,
            fiducials=tuple(Fiducial.from_dict(f) for f in truth.get("fiducials", ())),
        )
