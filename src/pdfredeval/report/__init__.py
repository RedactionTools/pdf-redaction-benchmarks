"""The reporter: scored runs in, something a person can read out.

    tables -> for the terminal and for a ticket
    plots  -> for the page
    page   -> one self-contained HTML file that survives being forwarded

Nothing here recomputes a probe outcome. It reads `score/probes.csv` and
`score/report.json` and aggregates, which keeps the published table reproducible from
the artefacts of a run that happened elsewhere, months ago, against a tool version that
no longer exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..types import Case
from ..workspace import output_pdf_path, resolve_case
from . import charts, html, overlay, tables
from .model import LoadedRun, NothingToReport, Report, load
from .palette import DARK, LIGHT
from .tables import Table, markdown_report, to_markdown, to_text

MARKDOWN_NAME = "report.md"
HTML_NAME = "report.html"
OVERLAY_DIR = "overlay"


@dataclass(frozen=True, slots=True)
class Written:
    """What a render left on disk."""

    markdown: Path | None = None
    page: Path | None = None
    images: tuple[Path, ...] = ()

    def paths(self) -> list[Path]:
        return [p for p in (self.markdown, self.page, *self.images) if p is not None]


def terminal_summary(report: Report, width: int = 96) -> str:
    """The short form: summary, headline, outcomes, leaks, and what was not measured.

    Deliberately not everything. The full breakdown is in the written report; a
    terminal that scrolls for two pages is one nobody reads to the end of.
    """
    blocks = [
        "Summary\n-------\n" + "\n".join(f"  - {line}" for line in tables.summary(report)),
    ]
    if len(report.runs) > 1:
        blocks.append(to_text(tables.comparison(report), width))
    blocks.extend([
        to_text(tables.headline(report), width),
        to_text(tables.counts(report), width),
    ])
    leaking = tables.layers(report)
    if not leaking.empty:
        blocks.append(to_text(leaking, width))
    coverage = tables.coverage(report)
    if not coverage.empty:
        blocks.append(to_text(coverage, width))
    notes = report.notes()
    if notes:
        blocks.append("Notes\n-----\n" + "\n".join(f"  - {n}" for n in notes))
    return "\n\n".join(block for block in blocks if block)


def render_overlays(
    report: Report,
    out_dir: Path | str,
    *,
    case_dir: Path | str | None = None,
    cases_root: Path | str = "cases",
) -> list[Path]:
    """Draw ground truth over each run's own output.

    Needs the case and the delivered bytes, which the score artefacts do not carry - so
    this is the one part of the reporter that reaches back to the run directory.
    """
    written: list[Path] = []
    for run in report.runs:
        source = run.path.parent
        output = output_pdf_path(
            source, run.case_id, run.tool_id, run.manifest.get("output_name")
        )
        if not output.exists():
            continue
        case_path = Path(case_dir) if case_dir else resolve_case(run.case_id, cases_root)
        if not (case_path / "ground_truth.json").exists():
            continue
        case = Case.from_dir(case_path, dataset_revision=run.dataset_revision)
        rows = _rows_with_boxes(run, case)
        drawn = overlay.render(
            case, output.read_bytes(), rows, out_dir, prefix=run.run_id,
        )
        written.extend(drawn.paths())
    return written


def _rows_with_boxes(run: LoadedRun, case: Case) -> list[dict[str, Any]]:
    """Join the scored outcomes back onto the ground-truth boxes they came from."""
    boxes = {probe.id: probe.bbox for probe in case.probes}
    rows = []
    for row in run.frame.itertuples():
        rows.append({
            "probe_id": row.probe_id,
            "outcome": row.outcome,
            "bbox": boxes.get(row.probe_id),
        })
    return rows


def write(
    report: Report,
    out_dir: Path | str,
    *,
    markdown: bool = True,
    page: bool = True,
    images: list[Path] | None = None,
) -> Written:
    """Write the report. Returns what landed where."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    markdown_path: Path | None = None
    if markdown:
        markdown_path = out / MARKDOWN_NAME
        markdown_path.write_text(markdown_report(report))

    page_path: Path | None = None
    if page:
        page_path = out / HTML_NAME
        page_path.write_text(html.render(report, images=images))

    return Written(markdown_path, page_path, tuple(images or ()))


__all__ = [
    "DARK",
    "HTML_NAME",
    "LIGHT",
    "MARKDOWN_NAME",
    "OVERLAY_DIR",
    "LoadedRun",
    "NothingToReport",
    "Report",
    "Table",
    "Written",
    "charts",
    "html",
    "load",
    "markdown_report",
    "overlay",
    "render_overlays",
    "tables",
    "terminal_summary",
    "to_markdown",
    "to_text",
    "write",
]
