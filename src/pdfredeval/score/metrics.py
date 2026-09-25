"""Every rate in docs/metrics/, as a function of the long table.

Two rules run through all of it.

**Pool counts; never average rates.** `LR(set of runs) = sum FN / sum (TP + FN)`.
Averaging per-case rates silently over-weights the cases carrying fewest probes, and the
cases carrying fewest probes are exactly the awkward ones.

**A rate without an interval is a claim without a error bar.** Wilson, because it stays
sane at p = 0 - and p = 0 is precisely where a good tool sits. Zero leaks in forty probes
is `[0, 0.088]`, not "100%".
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .rows import Outcome

Z = 1.96
BETA = 2.0


@dataclass(frozen=True, slots=True)
class Rate:
    """A proportion with the two numbers it came from, and its interval."""

    numerator: int
    denominator: int
    low: float
    high: float

    @property
    def value(self) -> float:
        return self.numerator / self.denominator if self.denominator else float("nan")

    @property
    def defined(self) -> bool:
        return self.denominator > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": None if not self.defined else round(self.value, 6),
            "n": self.denominator,
            "count": self.numerator,
            "ci95": [round(self.low, 6), round(self.high, 6)] if self.defined else None,
        }


def wilson(successes: int, trials: int, z: float = Z) -> tuple[float, float]:
    """Wilson score interval.

    The normal approximation collapses at `p = 0`, returning the degenerate `[0, 0]` -
    a 95% claim that a tool which leaked nothing in forty probes leaks nothing ever.
    """
    if trials <= 0:
        return (float("nan"), float("nan"))
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def rate(successes: int, trials: int, z: float = Z) -> Rate:
    low, high = wilson(successes, trials, z)
    return Rate(successes, trials, low, high)


# --- the table ------------------------------------------------------------------------


def _counts(frame: Any) -> dict[str, int]:
    values = frame["outcome"].value_counts().to_dict() if len(frame) else {}
    return {o.value: int(values.get(o.value, 0)) for o in Outcome}


#: Outcomes that are not cells of the TP/FN/FP/TN table and enter no rate.
EXCLUDED = (Outcome.UNSUPPORTED.value, Outcome.UNDECIDED.value)


def scored(frame: Any) -> Any:
    """Rows that enter a rate: decided, in scope, and not an arguable call."""
    if not len(frame):
        return frame
    return frame[~frame["outcome"].isin(EXCLUDED) & (~frame["ambiguous"])]


def leak_rate(frame: Any, z: float = Z) -> Rate:
    """`LR = FN / (TP + FN)` - of the targets, how many did the tool leave behind."""
    counts = _counts(scored(frame))
    return rate(counts["FN"], counts["FN"] + counts["TP"], z)


def over_redaction_rate(frame: Any, z: float = Z) -> Rate:
    """`ORR = FP / (FP + TN)` - of the distractors, how many did it destroy."""
    counts = _counts(scored(frame))
    return rate(counts["FP"], counts["FP"] + counts["TN"], z)


def weighted_leak_rate(frame: Any) -> float:
    """`wLR = sum_{m=1} w_i (1 - r_i) / sum_{m=1} w_i` - the same, by consequence."""
    targets = scored(frame)
    targets = targets[targets["must_redact"]] if len(targets) else targets
    total = float(targets["weight"].sum()) if len(targets) else 0.0
    if not total:
        return float("nan")
    leaked = float(targets.loc[~targets["removed"], "weight"].sum())
    return leaked / total


def area_over_coverage(mask: Any, boxes: Any) -> float:
    """`AOC = (area(M) - sum_{m=1} area(B_i)) / area(page)`.

    How much of the page was destroyed beyond what the targets themselves occupy. A
    tool that blacks out the sheet scores perfectly on privacy; this is the number that
    says what it cost.
    """
    if mask is None:
        return float("nan")
    target_area = sum(int(mask.area_px(box)) for box in boxes)
    changed = int(mask.changed_px)
    return max(0.0, (changed - target_area) / max(1, int(mask.page_area_px)))


def spill(frame: Any) -> float:
    """Mean overshoot of a redaction past its target."""
    if not len(frame):
        return float("nan")
    values = frame.loc[frame["must_redact"] & frame["spill"].notna(), "spill"]
    return float(values.mean()) if len(values) else float("nan")


def collateral(frame: Any, case: Any, epsilon_pt: float = 12.0) -> Rate:
    """Distractors destroyed *because they sat next to a target*.

    Separates two faults `ORR` alone conflates: a blunt brush that swallows whatever is
    beside the target, and a loose detector that redacts anything that looks sensitive
    anywhere on the page. Only the first is fixed by tightening the box.
    """
    boxes = {p.id: p.bbox for p in case.probes if p.bbox}
    targets = [p.bbox for p in case.targets if p.bbox]
    if not len(frame) or not targets:
        return rate(0, 0)

    near, destroyed = 0, 0
    for row in frame.itertuples():
        if row.must_redact or row.ambiguous or row.outcome in EXCLUDED:
            continue
        box = boxes.get(row.probe_id)
        if box is None:
            continue
        near += 1
        if row.outcome == Outcome.FP.value and any(
            _gap(box, target) < epsilon_pt for target in targets
        ):
            destroyed += 1
    return rate(destroyed, near)


def _gap(a: Any, b: Any) -> float:
    """Edge-to-edge distance between two boxes; 0 when they overlap."""
    dx = max(a.x0 - b.x1, b.x0 - a.x1, 0.0)
    dy = max(a.y0 - b.y1, b.y0 - a.y1, 0.0)
    return math.hypot(dx, dy)


def rqs(utility: float, privacy: float, beta: float = BETA) -> float:
    """`RQS_beta`, a weighted harmonic mean tilted toward privacy.

    Zero if either axis is zero, which is the whole point: redact-everything and
    do-nothing both score 0, and those are exactly the degenerate strategies the two
    axes exist to catch. A ranking convenience; `LR` and `ORR` are the result.
    """
    if math.isnan(utility) or math.isnan(privacy):
        return float("nan")
    denominator = beta * beta * utility + privacy
    if denominator <= 0:
        return 0.0
    return (1 + beta * beta) * utility * privacy / denominator


# --- breakdowns -----------------------------------------------------------------------


def by(frame: Any, column: str, z: float = Z) -> dict[str, dict[str, Any]]:
    """Leak and over-redaction rates split by any column. The mandatory breakdown."""
    rows = scored(frame)
    if not len(rows):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, group in rows.groupby(column, dropna=False):
        out[str(key)] = {
            "leak_rate": leak_rate(group, z).to_dict(),
            "over_redaction_rate": over_redaction_rate(group, z).to_dict(),
            "n": int(len(group)),
        }
    return out


def reach(frame: Any, z: float = Z) -> dict[str, dict[str, Any]]:
    """`Reach_k`: the redaction rate over probes reachable only through channel `k`.

    A floor on extraction, which is all a black box permits - and `Reach_k = 0` across a
    channel is a single-sentence finding: this tool does not read metadata.
    """
    rows = scored(frame)
    if not len(rows):
        return {}
    rows = rows[rows["must_redact"]]
    out: dict[str, dict[str, Any]] = {}
    for key, group in rows.groupby("channel", dropna=False):
        removed = int(group["removed"].sum())
        out[str(key)] = rate(removed, len(group), z).to_dict()
    return out


def condition_cost(frame: Any, axis: str) -> dict[str, float]:
    """`delta_x = Reach(control) - Reach_x` - the cost of a condition, not its rate.

    "This tool loses 0.6 on inverse backgrounds" is comparable across tools; "this tool
    scores 0.3 on inverse backgrounds" is not, because a tool that scores 0.3 everywhere
    has no inverse-background problem.
    """
    from ..types import Conditions

    control = getattr(Conditions(), axis)
    rows = scored(frame)
    if not len(rows):
        return {}
    rows = rows[rows["must_redact"]]
    if not len(rows):
        return {}

    baseline_rows = rows[rows[axis] == control]
    if not len(baseline_rows):
        return {}
    baseline = float(baseline_rows["removed"].mean())
    return {
        str(level): round(baseline - float(group["removed"].mean()), 6)
        for level, group in rows.groupby(axis, dropna=False)
    }


def layer_rates(frame: Any, layers: Sequence[str], z: float = Z) -> dict[str, dict[str, Any]]:
    """Layer Leak Rate and Exclusive Leak Rate, per layer.

    `LR` says a tool leaks. This says *how*, which is the part a vendor can fix - and
    the exclusive rate justifies each layer's place in the suite: a layer that never
    catches anything the others miss is dead weight and can be retired.
    """
    rows = scored(frame)
    rows = rows[rows["must_redact"]] if len(rows) else rows
    total = len(rows)
    out: dict[str, dict[str, Any]] = {}
    leaked_sets = (
        [set(filter(None, str(s).split(","))) for s in rows["leaked_layers"].fillna("")]
        if total else []
    )
    unread_sets = (
        [set(filter(None, str(s).split(",")))
         for s in rows["unavailable_layers"].fillna("")]
        if total else []
    )
    for layer in layers:
        caught = sum(1 for s in leaked_sets if layer in s)
        only = sum(1 for s in leaked_sets if s == {layer})
        unread = sum(1 for s in unread_sets if layer in s)
        out[layer] = {
            "layer_leak_rate": rate(caught, total, z).to_dict(),
            "exclusive_leak_rate": rate(only, total, z).to_dict(),
            "unavailable": unread,
        }
    return out


def agreement(frame: Any) -> dict[str, Any]:
    """For ambiguous probes: how often the tool's call matched ours, plus the list.

    Never folded into the headline. Burying a defensible disagreement inside one number
    is what makes a benchmark look arbitrary to the vendor it is measuring.
    """
    if not len(frame):
        return {"n": 0, "agreed": 0, "rate": None, "disagreements": []}
    rows = frame[frame["ambiguous"]]
    if not len(rows):
        return {"n": 0, "agreed": 0, "rate": None, "disagreements": []}
    agreed = rows["removed"] == rows["must_redact"]
    return {
        "n": int(len(rows)),
        "agreed": int(agreed.sum()),
        "rate": round(float(agreed.mean()), 6),
        "disagreements": [
            {"probe_id": r.probe_id, "category": r.category,
             "we_said": "redact" if r.must_redact else "keep",
             "tool": "redacted" if r.removed else "kept"}
            for r in rows[~agreed].itertuples()
        ],
    }


# --- across runs ----------------------------------------------------------------------


def bootstrap_pages(
    frames: Sequence[Any], *, draws: int = 2000, seed: int = 0
) -> tuple[float, float]:
    """Interval on a pooled rate by resampling *pages*, not probes.

    Probes on one page share a fate: one timeout, one mis-rotation, one skipped OCR pass
    fails all of them together. Treating forty clustered probes as forty independent
    trials overstates confidence, so with more than one page the interval comes from
    resampling pages with replacement.
    """
    if len(frames) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    samples = []
    for _ in range(draws):
        picked = [frames[rng.randrange(len(frames))] for _ in frames]
        fn = sum(_counts(scored(f))["FN"] for f in picked)
        tp = sum(_counts(scored(f))["TP"] for f in picked)
        if fn + tp:
            samples.append(fn / (fn + tp))
    if not samples:
        return (float("nan"), float("nan"))
    samples.sort()
    return (samples[int(0.025 * len(samples))], samples[int(0.975 * len(samples)) - 1])


def instability(frames: Sequence[Any]) -> float:
    """`| { i : r_i not constant across N attempts } | / |P|`.

    A tool at `LR = 0.1` with `Instability = 0.3` is not a 90% tool; it is a coin toss
    with a flattering average. Only observable on the API path, where a case can be run
    more than once.
    """
    if len(frames) < 2:
        return float("nan")
    outcomes: dict[str, set[bool]] = {}
    for frame in frames:
        for row in frame.itertuples():
            outcomes.setdefault(row.probe_id, set()).add(bool(row.removed))
    if not outcomes:
        return float("nan")
    unstable = sum(1 for values in outcomes.values() if len(values) > 1)
    return unstable / len(outcomes)


def pooled_leak_rate(frames: Sequence[Any], z: float = Z) -> Rate:
    """`LR` over a set of runs: sum the counts, then divide. Never a mean of rates."""
    fn = sum(_counts(scored(f))["FN"] for f in frames)
    tp = sum(_counts(scored(f))["TP"] for f in frames)
    return rate(fn, fn + tp, z)
