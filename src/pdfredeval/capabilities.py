"""What a tool claims to do, and what follows from what it does not.

Scoring a tool on capabilities it never claimed produces a misleading number, so probes
outside the declared scope are `unsupported`: reported, excluded from rates, never a
miss. The exclusion is not a pass - `coverage_statement()` is what keeps it honest.
See docs/adapters.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .types import (
    ORIENTATIONS,
    POLARITIES,
    PROVENANCES,
    Channel,
    Mode,
    Probe,
    ProbeKind,
    Stage,
)

# Object probe kinds, for mapping a probe to the category name a tool would declare.
_OBJECT_KINDS = {
    ProbeKind.FACE: "face",
    ProbeKind.SIGNATURE: "signature",
    ProbeKind.CODE: "code",
    ProbeKind.STAMP: "stamp",
}


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Declared scope. Defaults are deliberately narrow: claim, don't assume."""

    stages: frozenset[Stage] = frozenset({Stage.REDACTION})
    categories: frozenset[str] = frozenset()
    channels: frozenset[str] = frozenset({"text_layer"})
    per_type_control: bool = False
    max_pages: int | None = 1
    max_bytes: int | None = None
    formats: frozenset[str] = frozenset({"pdf"})
    mode: Mode = Mode.SYNC
    deterministic: bool = True
    # Rendering conditions the tool attempts. An axis left empty means "control only".
    orientations: frozenset[str] = frozenset({"horizontal"})
    polarities: frozenset[str] = frozenset({"normal"})
    provenances: frozenset[str] = frozenset({"vector", "print-clean"})

    def __post_init__(self) -> None:
        # Normalise anything passed as a bare set/list/tuple.
        for name in (
            "stages", "categories", "channels", "formats",
            "orientations", "polarities", "provenances",
        ):
            object.__setattr__(self, name, frozenset(getattr(self, name)))

    # --- scope -------------------------------------------------------------------

    def unsupported_reason(self, probe: Probe) -> str | None:
        """Why this probe is out of scope, or None if it is in scope.

        Returns a reason rather than a bool so the report can say *what* the tool does
        not attempt, which is the difference between an excuse and a finding.
        """
        if Stage.REDACTION not in self.stages:
            return "tool does not perform redaction"

        category = probe.category or _OBJECT_KINDS.get(probe.kind)
        if category and self.categories and category not in self.categories:
            return f"category {category!r} not supported"

        if probe.channel and self.channels and probe.channel.value not in self.channels:
            return f"channel {probe.channel.value!r} not supported"

        cond = probe.conditions
        if cond.orientation not in self.orientations:
            return f"orientation {cond.orientation!r} not supported"
        if cond.polarity not in self.polarities:
            return f"polarity {cond.polarity!r} not supported"
        if cond.provenance not in self.provenances:
            return f"provenance {cond.provenance!r} not supported"
        return None

    def supports(self, probe: Probe) -> bool:
        return self.unsupported_reason(probe) is None

    def accepts(self, *, pages: int, size_bytes: int, fmt: str = "pdf") -> str | None:
        """Why the input would be rejected, or None."""
        if self.formats and fmt not in self.formats:
            return f"format {fmt!r} not accepted"
        if self.max_pages is not None and pages > self.max_pages:
            return f"{pages} pages exceeds limit of {self.max_pages}"
        if self.max_bytes is not None and size_bytes > self.max_bytes:
            return f"{size_bytes} bytes exceeds limit of {self.max_bytes}"
        return None

    # --- honesty -----------------------------------------------------------------

    def coverage_statement(self) -> list[str]:
        """Plain-language list of what this tool does not attempt.

        Published beside the rates those probes are excluded from. A tool blind to
        handwriting will leave handwritten PII on a real document; the exclusion must
        never read as a pass.
        """
        gaps: list[str] = []
        missing_stages = {s.value for s in Stage} - {s.value for s in self.stages}
        if missing_stages:
            gaps.append("does not perform: " + ", ".join(sorted(missing_stages)))

        # Name the specific levels, not the axis. A tool that reads block capitals but
        # not cursive has a real gap, and "attempts handwriting" would paper over it.
        #
        # An empty declared set means *no restriction*, which is how `unsupported_reason`
        # reads it. Listing every level as missing would publish the exact opposite - a
        # tool that claimed no limit reported as attempting nothing.
        for declared, levels, label in (
            (self.channels, tuple(c.value for c in Channel), "channels"),
            (self.orientations, ORIENTATIONS, "orientations"),
            (self.polarities, POLARITIES, "polarities"),
            (self.provenances, PROVENANCES, "provenances"),
        ):
            if not declared:
                continue
            missing = [level for level in levels if level not in declared]
            if missing:
                gaps.append(f"does not attempt {label}: " + ", ".join(missing))

        if not self.deterministic:
            gaps.append("output is not deterministic; repeat runs to measure spread")
        return gaps

    def to_dict(self) -> dict[str, Any]:
        return {
            "stages": sorted(s.value for s in self.stages),
            "categories": sorted(self.categories),
            "channels": sorted(self.channels),
            "per_type_control": self.per_type_control,
            "max_pages": self.max_pages,
            "max_bytes": self.max_bytes,
            "formats": sorted(self.formats),
            "mode": self.mode.value,
            "deterministic": self.deterministic,
            "orientations": sorted(self.orientations),
            "polarities": sorted(self.polarities),
            "provenances": sorted(self.provenances),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Capabilities:
        return cls(
            stages=frozenset(Stage(s) for s in d.get("stages", ["redaction"])),
            categories=frozenset(d.get("categories", ())),
            channels=frozenset(d.get("channels", ["text_layer"])),
            per_type_control=bool(d.get("per_type_control", False)),
            max_pages=d.get("max_pages", 1),
            max_bytes=d.get("max_bytes"),
            formats=frozenset(d.get("formats", ["pdf"])),
            mode=Mode(d.get("mode", "sync")),
            deterministic=bool(d.get("deterministic", True)),
            orientations=frozenset(d.get("orientations", ["horizontal"])),
            polarities=frozenset(d.get("polarities", ["normal"])),
            provenances=frozenset(d.get("provenances", ["vector", "print-clean"])),
        )
