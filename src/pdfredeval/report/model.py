"""Reading scored runs back, and pooling them.

The reporter does not re-score anything. It reads what the scorer wrote - `probes.csv`
and `report.json` - which keeps one property worth having: a published table can be
regenerated from the artefacts of a run that happened on another machine, months ago,
with a tool that no longer exists in that version.

**Pool counts; never average rates.** `LR(set of runs) = sum FN / sum (TP + FN)`.
Averaging per-case rates silently over-weights the cases carrying fewest probes, which
are exactly the awkward ones. Every aggregate here concatenates the long tables and
recomputes from the counts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import BenchmarkError
from ..score import metrics
from ..score.scorer import REPORT_NAME, ROWS_NAME, SCORE_DIR
from ..thresholds import DEFAULTS, Thresholds


class NothingToReport(BenchmarkError):
    """No scored run was found where one was expected."""


@dataclass(frozen=True, slots=True)
class LoadedRun:
    """One scored run, as its own artefacts describe it."""

    path: Path
    frame: Any
    report: dict[str, Any]

    @property
    def run_id(self) -> str:
        return str(self.report.get("run_id") or self.path.parent.name)

    @property
    def case_id(self) -> str:
        return str(self.report.get("case_id") or "")

    @property
    def family(self) -> str | None:
        family = self.report.get("family")
        return str(family) if family else None

    @property
    def manifest(self) -> dict[str, Any]:
        return dict(self.report.get("manifest") or {})

    @property
    def tool_id(self) -> str:
        """A tool is `vendor:surface`; a run with no manifest is its own label."""
        return str(self.manifest.get("tool_id") or "unknown")

    @property
    def label(self) -> str:
        tier = self.manifest.get("tier")
        return f"{self.tool_id} ({tier})" if tier else self.tool_id

    @property
    def observed_at(self) -> str | None:
        stamp = self.manifest.get("observed_at")
        return str(stamp) if stamp else None

    @property
    def dataset_revision(self) -> str | None:
        revision = self.report.get("dataset_revision") or self.manifest.get(
            "dataset_revision"
        )
        return str(revision) if revision else None

    @property
    def summary(self) -> dict[str, Any]:
        return dict(self.report.get("summary") or {})

    @property
    def gate_failures(self) -> tuple[str, ...]:
        gates = (self.report.get("survivability") or {}).get("gates") or []
        return tuple(g["gate"] for g in gates if not g.get("passed"))

    @classmethod
    def load(cls, path: Path | str) -> LoadedRun:
        """Read a `score/` directory, or a run directory containing one."""
        path = Path(path)
        score = path if (path / ROWS_NAME).exists() else path / SCORE_DIR
        rows, report = score / ROWS_NAME, score / REPORT_NAME
        if not rows.exists() or not report.exists():
            raise NothingToReport(
                f"no scored run at {path}: expected {rows} and {report}. "
                f"Score it first: pdfredeval score {path}"
            )
        import pandas as pd

        return cls(
            path=score,
            frame=pd.read_csv(rows, keep_default_na=False, na_values=[""]),
            report=json.loads(report.read_text()),
        )


@dataclass(frozen=True, slots=True)
class Report:
    """One or more scored runs, ready to be rendered."""

    runs: tuple[LoadedRun, ...]
    title: str = "Redaction benchmark"

    def __post_init__(self) -> None:
        if not self.runs:
            raise NothingToReport("a report needs at least one scored run")

    # --- identity -------------------------------------------------------------------

    @property
    def single(self) -> LoadedRun | None:
        """The one run, when there is exactly one. Several tables key off this."""
        return self.runs[0] if len(self.runs) == 1 else None

    @property
    def thresholds(self) -> Thresholds:
        return Thresholds.from_dict(self.runs[0].report.get("thresholds") or {})

    @property
    def mixed_thresholds(self) -> bool:
        """Runs scored under different tables must not be pooled silently."""
        first = self.runs[0].report.get("thresholds")
        return any(r.report.get("thresholds") != first for r in self.runs[1:])

    @property
    def mixed_revisions(self) -> bool:
        first = self.runs[0].dataset_revision
        return any(r.dataset_revision != first for r in self.runs[1:])

    @property
    def pages(self) -> int:
        """Published beside the probe count, always: probes on one page share a fate."""
        return len({(r.case_id, r.run_id) for r in self.runs})

    @property
    def frame(self) -> Any:
        import pandas as pd

        return pd.concat([r.frame for r in self.runs], ignore_index=True)

    @property
    def frames(self) -> list[Any]:
        """One table per page - the resampling unit for the bootstrap."""
        return [r.frame for r in self.runs]

    # --- the numbers ----------------------------------------------------------------

    def headline(self) -> dict[str, Any]:
        """The pooled result. For a single run this reproduces its own summary."""
        frame = self.frame
        z = self.thresholds.z
        leak = metrics.leak_rate(frame, z)
        over = metrics.over_redaction_rate(frame, z)
        weighted = metrics.weighted_leak_rate(frame)
        privacy = 1 - weighted if weighted == weighted else float("nan")
        utility = 1 - over.value if over.defined else float("nan")

        # A single run's AOC and TR are page properties, not rates, so they are only
        # pooled as a mean - and said to be one.
        retentions = [
            float(r.summary["text_retention"]) for r in self.runs
            if r.summary.get("text_retention") is not None
        ]
        aocs = [
            float(r.summary["area_over_coverage"]) for r in self.runs
            if r.summary.get("area_over_coverage") is not None
        ]
        return {
            "leak_rate": leak,
            "over_redaction_rate": over,
            "weighted_leak_rate": weighted,
            "privacy": privacy,
            "utility": utility,
            "rqs": metrics.rqs(utility, privacy, self.thresholds.beta),
            "text_retention": sum(retentions) / len(retentions) if retentions else None,
            "area_over_coverage": sum(aocs) / len(aocs) if aocs else None,
            "probes": int(len(frame)),
            "pages": self.pages,
            "counts": (counts := self._counts(frame)),
            # TP and TN are the tool doing its job; FN and FP are the two ways it fails.
            # Unsupported, undecided and ambiguous probes are neither, and are counted
            # apart - the same rows the rates leave out.
            "passed": counts["TP"] + counts["TN"],
            "failed": counts["FN"] + counts["FP"],
            "not_scored": counts["unsupported"] + counts["undecided"] + counts["ambiguous"],
            "bootstrap": metrics.bootstrap_pages(self.frames) if self.pages > 1 else None,
            "instability": self._instability(),
        }

    def _counts(self, frame: Any) -> dict[str, int]:
        from ..score.rows import Outcome

        rows = metrics.scored(frame)
        values = rows["outcome"].value_counts().to_dict() if len(rows) else {}
        counts = {o.value: int(values.get(o.value, 0)) for o in Outcome if o.scored}
        everything = frame["outcome"].value_counts().to_dict() if len(frame) else {}
        for held in (Outcome.UNSUPPORTED, Outcome.UNDECIDED):
            counts[held.value] = int(everything.get(held.value, 0))
        counts["ambiguous"] = int(
            (frame["ambiguous"] & ~frame["outcome"].isin(metrics.EXCLUDED)).sum()
        ) if len(frame) else 0
        return counts

    def _instability(self) -> float | None:
        """Only meaningful across repeats of the *same* case by the same tool."""
        groups: dict[tuple[str, str], list[Any]] = {}
        for run in self.runs:
            groups.setdefault((run.tool_id, run.case_id), []).append(run.frame)
        repeats = [frames for frames in groups.values() if len(frames) > 1]
        if not repeats:
            return None
        values = [metrics.instability(frames) for frames in repeats]
        usable = [v for v in values if v == v]
        return sum(usable) / len(usable) if usable else None

    def by_tool(self) -> list[dict[str, Any]]:
        """One pooled row per tool. The comparison table, and the scatter's points."""
        groups: dict[str, list[LoadedRun]] = {}
        for run in self.runs:
            groups.setdefault(run.label, []).append(run)

        rows = []
        for label, runs in sorted(groups.items()):
            pooled = Report(tuple(runs), title=label)
            head = pooled.headline()
            rows.append({
                "label": label,
                "tool_id": runs[0].tool_id,
                "runs": len(runs),
                "pages": pooled.pages,
                "probes": head["probes"],
                "leak_rate": head["leak_rate"],
                "over_redaction_rate": head["over_redaction_rate"],
                "weighted_leak_rate": head["weighted_leak_rate"],
                "privacy": head["privacy"],
                "utility": head["utility"],
                "rqs": head["rqs"],
                "text_retention": head["text_retention"],
                "gates": sorted({g for r in runs for g in r.gate_failures}),
                "instability": head["instability"],
            })
        return rows

    # --- what was not measured ------------------------------------------------------

    def coverage_gaps(self) -> list[str]:
        """What the tools do not attempt, from the capabilities they declared.

        Published beside the rates those probes were excluded from. A tool blind to
        handwriting will leave handwritten PII on a real document, so an exclusion has
        to read as a gap rather than as a pass.
        """
        from ..capabilities import Capabilities

        per_tool: dict[str, list[str]] = {}
        for run in self.runs:
            declared = run.manifest.get("capabilities")
            found = per_tool.setdefault(run.label, [])
            if not declared:
                continue
            for gap in Capabilities.from_dict(declared).coverage_statement():
                if gap not in found:
                    found.append(gap)
        return _shared_first(per_tool, "all tools")

    def unavailable_layers(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for run in self.runs:
            for layer, why in (run.report.get("unavailable_layers") or {}).items():
                found.setdefault(layer, why)
        return found

    def notes(self) -> list[str]:
        found: list[str] = []
        if self.mixed_thresholds:
            found.append(
                "these runs were scored under different threshold tables; change a "
                "threshold and you have changed the benchmark, so they are not "
                "comparable and must not be pooled"
            )
        if self.mixed_revisions:
            found.append(
                "these runs cite different dataset revisions; a score is only "
                "comparable against the revision it was computed on"
            )
        per_run = {
            run.run_id: [str(note) for note in run.report.get("notes") or ()]
            for run in self.runs
        }
        found.extend(line for line in _shared_first(per_run, "all runs")
                     if line not in found)
        return found


def _shared_first(lines: dict[str, list[str]], everyone: str) -> list[str]:
    """Say a line once when every owner has it; prefix it with its owner otherwise."""
    if len(lines) == 1:
        return list(next(iter(lines.values())))
    shared = [line for line in next(iter(lines.values()))
              if all(line in others for others in lines.values())]
    out = [f"{everyone}: {line}" for line in shared]
    for owner, owned in lines.items():
        out.extend(f"{owner}: {line}" for line in owned if line not in shared)
    return out


def load(paths: list[Path | str], *, title: str | None = None) -> Report:
    """Load every scored run under the given run, score or parent directories."""
    runs: list[LoadedRun] = []
    for path in paths:
        path = Path(path)
        if (path / ROWS_NAME).exists() or (path / SCORE_DIR / ROWS_NAME).exists():
            runs.append(LoadedRun.load(path))
            continue
        # A parent directory: take every scored run underneath it, so `report runs/`
        # does the obvious thing.
        found = sorted(
            child for child in path.glob("*")
            if child.is_dir() and (child / SCORE_DIR / ROWS_NAME).exists()
        )
        if not found:
            raise NothingToReport(
                f"no scored run at {path} or in it. Score one first: "
                f"pdfredeval score {path}"
            )
        runs.extend(LoadedRun.load(child) for child in found)

    if title is None:
        title = runs[0].label if len({r.label for r in runs}) == 1 else "Tool comparison"
    return Report(tuple(runs), title=title)


__all__ = ["DEFAULTS", "LoadedRun", "NothingToReport", "Report", "load"]
