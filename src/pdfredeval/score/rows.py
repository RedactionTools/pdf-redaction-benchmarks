"""One row per probe. The single decision the whole metric layer rests on.

docs/metrics/core.md: "One long table. Every metric below is a groupby over it; every
plot is a pivot. Nothing is aggregated before it lands here." Keeping the row flat - one
column per layer, the condition axes spread out rather than nested - is what makes
`df.groupby("category").outcome.value_counts()` the entire implementation of the
per-category breakdown the doc calls mandatory.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from ..probe.base import Layer
from .rules import LayerResult, Verdict


class Outcome(str, Enum):
    """Where a scoped probe lands. docs/metrics/core.md."""

    TP = "TP"  # must redact, removed
    FN = "FN"  # must redact, kept - a leak, the privacy failure
    FP = "FP"  # must survive, removed - over-redaction, a utility loss
    TN = "TN"  # must survive, kept
    UNSUPPORTED = "unsupported"  # outside the tool's declared scope; never a miss
    #: Not a cell in the table: no layer reached a verdict at all, because none of them
    #: could be read. Distinct from `unsupported`, which is about what the tool claims;
    #: this is about what we managed to look at. Calling it "removed" would hand a
    #: perfect leak rate to an output nobody could open.
    UNDECIDED = "undecided"

    @property
    def scored(self) -> bool:
        return self not in (Outcome.UNSUPPORTED, Outcome.UNDECIDED)


@dataclass(frozen=True, slots=True)
class ProbeRow:
    """The record for one probe in one run. Nothing is lost at this level."""

    run_id: str
    case_id: str
    probe_id: str
    kind: str
    category: str | None
    severity: str
    weight: int
    difficulty: str
    must_redact: bool
    ambiguous: bool
    outcome: str
    removed: bool
    scope: str  # "in_scope" | "unsupported"
    unsupported_reason: str | None = None
    channel: str | None = None
    trap: str | None = None
    orientation: str = "horizontal"
    polarity: str = "normal"
    provenance: str = "vector"
    scale: str = "pt10"
    # --- measurements, for grading and for the condition breakdowns ---------------
    rdr: float = 0.0
    coverage: float | None = None
    spill: float | None = None
    grade: str = "redacted"
    leaked_layers: str = ""
    unavailable_layers: str = ""
    decided_layers: int = 0
    complete: bool = True
    page: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _flatten(value: Any) -> Any:
    """Columns must be scalar; the evidence dictionary is carried as JSON."""
    import json

    return json.dumps(value, sort_keys=True, default=str) if isinstance(value, dict) else value


def to_frame(rows: Sequence[ProbeRow]) -> Any:
    """The long table, as pandas. The only place this package needs a DataFrame."""
    import pandas as pd

    if not rows:
        return pd.DataFrame(columns=[f for f in ProbeRow.__slots__])
    return pd.DataFrame([
        {k: _flatten(v) for k, v in row.to_dict().items()} for row in rows
    ])


def summarise_layers(results: dict[Layer, LayerResult]) -> dict[str, Any]:
    """The per-layer columns of a row, derived once."""
    leaked = [layer.value for layer, r in results.items() if r.verdict is Verdict.LEAKED]
    unread = [layer.value for layer, r in results.items()
              if r.verdict is Verdict.UNAVAILABLE]
    decided = sum(1 for r in results.values() if r.verdict.decided)
    return {
        "leaked_layers": ",".join(sorted(leaked)),
        "unavailable_layers": ",".join(sorted(unread)),
        "decided_layers": decided,
        # A probe called clean while a layer went unread is not the same finding as one
        # checked on every surface. The report says how many rows are in that state.
        "complete": not unread,
    }
