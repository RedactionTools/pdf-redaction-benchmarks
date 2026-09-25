"""The scorer: one run in, one row per probe out.

Align once, probe once, decide eighty times. The expensive work - two 300 DPI renders and
an OCR sweep - is shared across every probe on the page, which is the whole reason probe
packing pays for itself.

Every judgement this file makes is a lookup in docs/metrics/core.md:

* scope first - a probe outside the tool's declared capabilities is `unsupported`,
  reported and excluded from every rate, never counted as a miss;
* `leak_i = OR over layers`, so the worst layer sets the outcome;
* `r_i = 1 - leak_i`, and the TP/FN/FP/TN table does the rest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..capabilities import Capabilities
from ..engines import versions as engine_versions
from ..errors import BenchmarkError
from ..manifest import RunManifest
from ..probe import Observations, probe
from ..probe.base import Layer
from ..thresholds import DEFAULTS, Thresholds
from ..types import ORIENTATIONS, POLARITIES, PROVENANCES, Case, Probe, Stage
from ..workspace import output_pdf_path, resolve_case
from . import detection, metrics
from .detection import DetectionReport
from .rows import Outcome, ProbeRow, summarise_layers, to_frame
from .rules import LayerResult, Verdict, evaluate

SCORE_DIR = "score"
ROWS_NAME = "probes.csv"
REPORT_NAME = "report.json"


class ScoringError(BenchmarkError):
    """The run cannot be scored as delivered."""


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Everything one scored run knows about itself."""

    rows: tuple[ProbeRow, ...]
    case: Case
    manifest: RunManifest | None
    observations: Observations
    thresholds: Thresholds
    detection: DetectionReport
    layer_results: dict[str, dict[Layer, LayerResult]] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def frame(self) -> Any:
        """The long table. Every metric is a groupby over this."""
        return to_frame(self.rows)

    # --- the headline ---------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        frame = self.frame()
        leak = metrics.leak_rate(frame, self.thresholds.z)
        over = metrics.over_redaction_rate(frame, self.thresholds.z)
        weighted = metrics.weighted_leak_rate(frame)
        privacy = 1 - weighted if weighted == weighted else float("nan")
        utility = 1 - over.value if over.defined else float("nan")
        boxes = [p.bbox for p in self.case.targets if p.bbox]
        aoc = metrics.area_over_coverage(self.observations.mask, boxes)
        retention = self.observations.survivability.text_retention

        return {
            "leak_rate": leak.to_dict(),
            "weighted_leak_rate": _clean(weighted),
            "over_redaction_rate": over.to_dict(),
            "area_over_coverage": _clean(aoc),
            "text_retention": round(retention, 6),
            "spill": _clean(metrics.spill(frame)),
            "collateral": metrics.collateral(
                frame, self.case, self.thresholds.spill_epsilon_pt
            ).to_dict(),
            "privacy": _clean(privacy),
            "utility": _clean(utility),
            "rqs": _clean(metrics.rqs(utility, privacy, self.thresholds.beta)),
            "counts": _outcome_counts(frame),
            "probes": len(self.rows),
            "pages": 1,
        }

    def report(self) -> dict[str, Any]:
        """The published record: the numbers, and everything needed to argue with them."""
        frame = self.frame()
        return {
            "run_id": self.rows[0].run_id if self.rows else None,
            "case_id": self.case.case_id,
            "family": self.case.family,
            "dataset_revision": self.case.dataset_revision,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "thresholds": self.thresholds.to_dict(),
            "engines": self.observations.engines or engine_versions(),
            "alignment": self.observations.alignment.to_dict(),
            "survivability": self.observations.survivability.to_dict(),
            "summary": self.summary(),
            "by_category": metrics.by(frame, "category", self.thresholds.z),
            "by_severity": metrics.by(frame, "severity", self.thresholds.z),
            "by_difficulty": metrics.by(frame, "difficulty", self.thresholds.z),
            "by_kind": metrics.by(frame, "kind", self.thresholds.z),
            "by_trap": metrics.by(frame, "trap", self.thresholds.z),
            "reach": metrics.reach(frame, self.thresholds.z),
            "condition_cost": {
                axis: metrics.condition_cost(frame, axis)
                for axis in ("orientation", "polarity", "provenance", "scale")
            },
            "layers": _layer_section(frame, self.thresholds.z),
            "detection": self.detection.to_dict(),
            "ambiguous": metrics.agreement(frame),
            "unsupported": _unsupported(self.rows),
            "undecided": sum(1 for r in self.rows
                             if r.outcome == Outcome.UNDECIDED.value),
            "unavailable_layers": self.observations.unavailable_layers,
            "incomplete_rows": sum(1 for r in self.rows if not r.complete),
            "notes": list(self.notes),
        }

    def write(self, out_dir: Path | str) -> Path:
        """Write `probes.csv` and `report.json` into a run's `score/` directory."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        self.frame().to_csv(out / ROWS_NAME, index=False)
        (out / REPORT_NAME).write_text(
            json.dumps(self.report(), indent=2, sort_keys=True, default=str) + "\n"
        )
        return out


def _layer_section(frame: Any, z: float) -> dict[str, dict[str, Any]]:
    """The leak-layer vector, each entry carrying what a survival there *means*.

    A rate on its own does not tell a reader that a value left in an earlier revision is
    trivially recovered while one left in a font subset only narrows the guess. The
    severity and the consequence travel with the number.
    """
    rates = metrics.layer_rates(frame, [layer.value for layer in Layer], z)
    return {
        layer.value: {
            **rates[layer.value],
            "severity": layer.severity.value,
            "if_it_survives": layer.description,
        }
        for layer in Layer
    }


def _clean(value: float) -> float | None:
    """NaN is not JSON. An undefined rate is `null`, which is what it means."""
    return None if value != value else round(float(value), 6)


def _outcome_counts(frame: Any) -> dict[str, int]:
    """The outcome table the rates are computed from.

    Counted over the *scored* rows, so the cells add up to the denominators of `LR` and
    `ORR`. Ambiguous probes are held out of both and reported beside them, never folded
    in - a count that silently included them would not reconcile with its own rates.
    """
    rows = metrics.scored(frame)
    values = rows["outcome"].value_counts().to_dict() if len(rows) else {}
    counts = {o.value: int(values.get(o.value, 0)) for o in Outcome if o.scored}
    # The three held-out populations are counted over the whole table, since `scored`
    # is precisely what removed them. Together with the four cells they must add up to
    # the probe count, or the report does not reconcile with itself.
    everything = frame["outcome"].value_counts().to_dict() if len(frame) else {}
    counts[Outcome.UNSUPPORTED.value] = int(everything.get(Outcome.UNSUPPORTED.value, 0))
    counts[Outcome.UNDECIDED.value] = int(everything.get(Outcome.UNDECIDED.value, 0))
    # Ambiguity is a property, not an outcome, so an arguable probe that was also out of
    # scope or undecidable is already counted there. Counting it twice would leave the
    # table adding up to more probes than the page carries.
    counts["ambiguous"] = int(
        (frame["ambiguous"] & ~frame["outcome"].isin(metrics.EXCLUDED)).sum()
    ) if len(frame) else 0
    return counts


def _unsupported(rows: Sequence[ProbeRow]) -> dict[str, Any]:
    """What the tool never claimed to do, reported beside the rates it is excluded from.

    An exclusion must never read as a pass: a tool blind to handwriting will leave
    handwritten PII on a real document.
    """
    excluded = [r for r in rows if r.outcome == Outcome.UNSUPPORTED.value]
    reasons: dict[str, int] = {}
    for row in excluded:
        reasons[row.unsupported_reason or "unsupported"] = (
            reasons.get(row.unsupported_reason or "unsupported", 0) + 1
        )
    return {"n": len(excluded), "reasons": reasons}


#: Used only when a run records no declared scope. Wide open on purpose: an empty
#: category or channel set means "no restriction", so nothing is excluded - see `score`.
EVERYTHING = Capabilities(
    stages=frozenset(Stage),
    categories=frozenset(),
    channels=frozenset(),
    orientations=frozenset(ORIENTATIONS),
    polarities=frozenset(POLARITIES),
    provenances=frozenset(PROVENANCES),
)


# --- scoring one probe ----------------------------------------------------------------


def outcome_for(*, must_redact: bool, removed: bool) -> Outcome:
    """The table in docs/metrics/core.md, as code."""
    if must_redact:
        return Outcome.TP if removed else Outcome.FN
    return Outcome.FP if removed else Outcome.TN


def score_probe(
    item: Probe,
    observations: Observations,
    capabilities: Capabilities,
    thresholds: Thresholds,
    *,
    run_id: str,
    case_id: str,
) -> tuple[ProbeRow, dict[Layer, LayerResult]]:
    """One probe, one row."""
    reason = capabilities.unsupported_reason(item)
    results = evaluate(item, observations, thresholds)
    summary = summarise_layers(results)

    leaked = [layer for layer, r in results.items() if r.verdict is Verdict.LEAKED]
    # A probe every layer failed to read is not a redacted probe. "No leak found" from
    # checks that never ran is how an unopenable output would score a perfect privacy
    # rate, so it gets an outcome of its own and enters no rate.
    decided = any(r.verdict.decided for r in results.values())
    removed = decided and not leaked
    if reason:
        outcome = Outcome.UNSUPPORTED
    elif not decided:
        outcome = Outcome.UNDECIDED
    else:
        outcome = outcome_for(must_redact=item.must_redact, removed=removed)

    pixels = results[Layer.RENDERED_PIXELS]
    worst = max(
        (r for r in results.values() if r.verdict is Verdict.LEAKED),
        key=lambda r: float(r.evidence.get("rdr", 0.0)),
        default=None,
    )
    rdr = max(
        (float(r.evidence.get("rdr", 0.0)) for r in results.values()
         if "rdr" in r.evidence),
        default=0.0,
    )

    row = ProbeRow(
        run_id=run_id,
        case_id=case_id,
        probe_id=item.id,
        kind=item.kind.value,
        category=item.category,
        severity=item.severity.value,
        weight=item.weight,
        difficulty=item.difficulty.value,
        must_redact=item.must_redact,
        ambiguous=item.ambiguous,
        outcome=outcome.value,
        removed=removed,
        scope="unsupported" if reason else "in_scope",
        unsupported_reason=reason,
        channel=item.channel.value if item.channel else None,
        trap=item.trap,
        orientation=item.conditions.orientation,
        polarity=item.conditions.polarity,
        provenance=item.conditions.provenance,
        scale=item.conditions.scale,
        rdr=round(rdr, 4),
        coverage=pixels.evidence.get("coverage"),
        spill=pixels.evidence.get("spill"),
        grade=(worst.evidence.get("grade", "leaked") if worst else "redacted"),
        page=item.bbox.page if item.bbox else 0,
        evidence={
            layer.value: result.to_dict()
            for layer, result in results.items()
            if result.verdict is not Verdict.NOT_APPLICABLE or result.reason
        },
        **summary,
    )
    return (row, results)


# --- scoring one run --------------------------------------------------------------------


def score(
    case: Case,
    output_pdf: bytes,
    *,
    manifest: RunManifest | None = None,
    capabilities: Capabilities | None = None,
    thresholds: Thresholds = DEFAULTS,
    entities: list[detection.Entity] | None = None,
    run_id: str | None = None,
    ocr_enabled: bool = True,
) -> ScoreResult:
    """Score one delivered output against its case."""
    notes: list[str] = []
    if capabilities is None:
        if manifest and manifest.capabilities:
            capabilities = Capabilities.from_dict(manifest.capabilities)
        else:
            # Assuming the narrow default would silently mark most of the page
            # unsupported and publish a flattering rate over the remainder. Scoring
            # everything and saying so is the honest failure.
            capabilities = EVERYTHING
            notes.append(
                "the run declares no capabilities, so every probe was scored in scope; "
                "a tool is not credited here for anything it never claimed to do"
            )

    observations = probe(case, output_pdf, thresholds=thresholds, ocr_enabled=ocr_enabled)
    identifier = run_id or (manifest.run_id if manifest else f"adhoc-{case.case_id}")

    rows: list[ProbeRow] = []
    layer_results: dict[str, dict[Layer, LayerResult]] = {}
    for item in case.probes:
        row, results = score_probe(
            item, observations, capabilities, thresholds,
            run_id=identifier, case_id=case.case_id,
        )
        rows.append(row)
        layer_results[item.id] = results

    for layer, why in observations.unavailable_layers.items():
        notes.append(f"layer {layer} could not be read: {why}")
    if not observations.alignment.confident:
        notes.append(observations.alignment.note or "alignment is not trustworthy")

    return ScoreResult(
        rows=tuple(rows),
        case=case,
        manifest=manifest,
        observations=observations,
        thresholds=thresholds,
        detection=detection.report(
            entities, case,
            tau_span=thresholds.tau_span, tau_iou=thresholds.tau_iou,
        ),
        layer_results=layer_results,
        notes=tuple(notes),
    )


def score_run(
    run_dir: Path | str,
    *,
    case_dir: Path | str | None = None,
    cases_root: Path | str = "cases",
    thresholds: Thresholds = DEFAULTS,
    ocr_enabled: bool = True,
    verify: bool = True,
) -> ScoreResult:
    """Score a collected run directory: `manifest.json` + the output PDF it names.

    The checksums are verified by default. A manifest exists to prove the scored bytes
    are the delivered bytes; scoring something else while quoting its manifest would
    publish a number about a file nobody submitted.
    """
    run = Path(run_dir)
    manifest_path = run / "manifest.json"
    if not manifest_path.exists():
        raise ScoringError(
            f"{manifest_path} not found. Collect the run first: pdfredeval collect {run}"
        )
    manifest = RunManifest.load(manifest_path)
    output_path = output_pdf_path(
        run, manifest.case_id, manifest.tool_id, manifest.output_name
    )
    if not output_path.exists():
        raise ScoringError(f"{output_path} not found; nothing was delivered for this run")

    case = _load_case(manifest.case_id, case_dir, cases_root, manifest.dataset_revision)
    output_pdf = output_path.read_bytes()

    if verify:
        _verify(manifest, case, output_pdf)

    return score(
        case, output_pdf,
        manifest=manifest,
        thresholds=thresholds,
        entities=detection.load_entities(run),
        ocr_enabled=ocr_enabled,
    )


def _load_case(
    case_id: str,
    case_dir: Path | str | None,
    cases_root: Path | str,
    revision: str | None,
) -> Case:
    path = Path(case_dir) if case_dir else resolve_case(case_id, cases_root)
    if not (path / "ground_truth.json").exists():
        raise ScoringError(
            f"no ground truth for case {case_id!r} at {path}. Pass --case-dir, or "
            f"regenerate it: pdfredeval generate <family> --seed <n> -o {cases_root}"
        )
    return Case.from_dir(path, dataset_revision=revision)


def _verify(manifest: RunManifest, case: Case, output_pdf: bytes) -> None:
    digest = hashlib.sha256(output_pdf).hexdigest()
    if manifest.output_sha256 and manifest.output_sha256 != digest:
        raise ScoringError(
            f"the output PDF does not match the manifest: {digest[:12]} on disk, "
            f"{manifest.output_sha256[:12]} recorded. The delivered bytes have changed - "
            f"opening and re-saving an output in any viewer is enough to do it, and it "
            f"strips exactly the artefacts the leak checks hunt for."
        )
    if manifest.input_sha256 and manifest.input_sha256 != case.sha256:
        raise ScoringError(
            f"case {case.case_id} does not match the one submitted: {case.sha256[:12]} "
            f"on disk, {manifest.input_sha256[:12]} recorded. Regenerate it with the "
            f"seed and --generated-at from its ground truth."
        )
