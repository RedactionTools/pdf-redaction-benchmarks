"""The tables. One builder per breakdown, three renderers.

docs/metrics/core.md calls the per-category split **mandatory**: "94% overall" hides "0%
on IBANs". The same argument runs through every table here - extraction.md asks for the
condition vector and "never the mean", redaction.md for the leak-layer vector,
detection.md for the difficulty tiers. A reporter that prints one number per run would
be discarding the part a vendor can act on.

Every rate is printed with its interval and its `n`. A bare `0.0` over four probes and a
`0.0` over four hundred are different claims, and only one of them is worth publishing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..probe.base import Layer
from ..score import metrics
from ..score.rows import Outcome
from .model import Report

#: Column alignment. Numbers right, words left - so a column of rates reads as a column.
LEFT, RIGHT = "left", "right"


@dataclass(frozen=True, slots=True)
class Table:
    """A rendered-once, printed-three-ways block."""

    key: str
    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    align: tuple[str, ...] = ()
    note: str = ""
    #: Rows carry an optional status role per cell, so a gate or an outcome can show a
    #: glyph beside its colour instead of relying on the colour alone.
    roles: tuple[tuple[str | None, ...], ...] = field(default=())

    @property
    def empty(self) -> bool:
        return not self.rows

    def alignment(self) -> tuple[str, ...]:
        return self.align or tuple(
            LEFT if i == 0 else RIGHT for i in range(len(self.columns))
        )


# --- formatting -----------------------------------------------------------------------


def fmt(value: Any, digits: int = 3) -> str:
    """A number, or `n/a` - never a silent zero standing in for "undefined"."""
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number:
        return "n/a"
    return f"{number:.{digits}f}"


def fmt_rate(rate: Any, *, with_n: bool = True) -> str:
    """`0.019 [0.00, 0.10] n=54`. The interval and the `n` are not optional."""
    if rate is None:
        return "n/a"
    value = rate.value if hasattr(rate, "value") else rate.get("value")
    if value is None or value != value:
        return "n/a"
    low, high = (
        (rate.low, rate.high) if hasattr(rate, "low")
        else tuple(rate.get("ci95") or (None, None))
    )
    n = rate.denominator if hasattr(rate, "denominator") else rate.get("n")
    text = fmt(value)
    if low is not None:
        text += f" [{float(low):.2f}, {float(high):.2f}]"
    if with_n and n is not None:
        text += f" n={n}"
    return text


# --- builders -------------------------------------------------------------------------


def headline(report: Report) -> Table:
    """Both axes, leak rate first. An under-redaction discloses data; an
    over-redaction only wastes a page."""
    head = report.headline()
    rows = [
        ("Leak rate (LR)", fmt_rate(head["leak_rate"]), "0 is best",
         "share of sensitive values still recoverable from the output"),
        ("Over-redaction rate (ORR)", fmt_rate(head["over_redaction_rate"]), "0 is best",
         "share of values that should have stayed but were removed"),
        ("Weighted leak rate (wLR)", fmt(head["weighted_leak_rate"]), "0 is best",
         "leak rate with severe categories counting more"),
        ("Text retention (TR)", fmt(head["text_retention"]), "1 is best",
         "share of the page's text still selectable - is the document usable"),
        ("Area over-coverage (AOC)", fmt(head["area_over_coverage"], 4), "0 is best",
         "share of the page blacked out that did not need to be"),
        ("RQS (beta=2)", fmt(head["rqs"]), "1 is best",
         "one-number score for sorting only; LR and ORR are the result"),
    ]
    if head["bootstrap"] is not None and head["bootstrap"][0] == head["bootstrap"][0]:
        low, high = head["bootstrap"]
        rows.append((
            "LR range across pages", f"[{low:.2f}, {high:.2f}]", "narrow is better",
            "how much the leak rate moves from page to page",
        ))
    if head["instability"] is not None:
        rows.append((
            "Instability", fmt(head["instability"]), "0 is best",
            "share of probes that got a different result when the same page was re-run",
        ))
    return Table(
        key="headline",
        title="Headline",
        columns=("Metric", "Value", "Better", "What it means"),
        rows=tuple(rows),
        align=(LEFT, RIGHT, LEFT, LEFT),
        note="A rate reads `value [low, high] n=count`: the value, the range the true "
             "rate lies in with 95% confidence, and how many probes it rests on.",
    )


def counts(report: Report) -> Table:
    """The outcome table. The cells and the held-out populations sum to the probes."""
    head = report.headline()
    cells = head["counts"]
    meaning = {
        Outcome.TP.value: ("Removed, as it should be", "sensitive value gone - good"),
        Outcome.FN.value: ("Leaked", "sensitive value still recoverable - the privacy "
                                     "failure"),
        Outcome.FP.value: ("Removed by mistake", "harmless value destroyed - a utility "
                                                 "loss"),
        Outcome.TN.value: ("Kept, as it should be", "harmless value left intact - good"),
        Outcome.UNSUPPORTED.value: ("Out of scope", "the tool says it does not handle "
                                                    "this; not counted as a miss"),
        Outcome.UNDECIDED.value: ("Could not check", "no layer could be read; left out "
                                                     "of every rate"),
        "ambiguous": ("Arguable", "reasonable people could disagree; scored apart"),
    }
    return Table(
        key="counts",
        title="Outcomes",
        columns=("Outcome", "Probes", "Meaning"),
        rows=tuple(
            (f"{label} ({name})", str(cells.get(name, 0)), text)
            for name, (label, text) in meaning.items()
        ),
        align=(LEFT, RIGHT, LEFT),
        roles=tuple(
            (None, _outcome_role(name, cells.get(name, 0)), None)
            for name in meaning
        ),
        note=f"Every probe lands in exactly one row: {sum(cells.values())} of "
             f"{head['probes']}. Leak rate = Leaked / (Leaked + Removed); "
             f"over-redaction = Removed by mistake / (Removed by mistake + Kept).",
    )


def _outcome_role(name: str, count: int) -> str | None:
    if not count:
        return None
    return {
        Outcome.FN.value: "critical",
        Outcome.FP.value: "warning",
        Outcome.TP.value: "good",
        Outcome.UNDECIDED.value: "serious",
    }.get(name)


def gates(report: Report) -> Table:
    """Survivability. Published as flags beside the scores, never folded into them."""
    rows: list[tuple[str, ...]] = []
    roles: list[tuple[str | None, ...]] = []
    many = len(report.runs) > 1
    for run in report.runs:
        found = (run.report.get("survivability") or {}).get("gates") or []
        failed = [gate for gate in found if not gate.get("passed")]
        if found and not failed:
            rows.append((
                run.run_id if many else "all gates", "pass",
                "all passed: " + ", ".join(str(gate["gate"]) for gate in found),
            ))
            roles.append((None, "good", None))
        for gate in failed:
            label = f"{run.run_id} · {gate['gate']}" if many else str(gate["gate"])
            meaning = GATE_MEANING.get(str(gate["gate"]), "")
            detail = str(gate.get("detail") or "")
            rows.append((label, "FAIL", f"{meaning} ({detail})" if meaning else detail))
            roles.append((None, "critical", None))
    return Table(
        key="gates",
        title="Is the document still usable?",
        columns=("Check", "Result", "What it means"),
        rows=tuple(rows),
        align=(LEFT, LEFT, LEFT),
        roles=tuple(roles),
        note="These checks do not change the scores; a failure is a warning next to "
             "them. Turning the page into a picture hides every value and also "
             "destroys the document, and these checks are what catch that.",
    )


#: What a failed gate means for the person holding the output.
GATE_MEANING = {
    "opens": "the output does not open as a PDF",
    "pages": "the output has a different number of pages",
    "geometry": "the page size changed",
    "text_retained": "too little of the page's text can still be selected or searched",
    "not_rasterised": "the page was turned into an image; text is no longer "
                      "selectable or searchable",
}


def _breakdown(report: Report, column: str, title: str, note: str = "") -> Table:
    split = metrics.by(report.frame, column, report.thresholds.z)
    rows = tuple(
        ("none" if key == "nan" else key, fmt_rate(stats["leak_rate"]),
         fmt_rate(stats["over_redaction_rate"]),
         str(stats["n"]))
        for key, stats in sorted(split.items())
    )
    return Table(
        key=f"by_{column}",
        title=title,
        columns=(column.replace("_", " ").capitalize(), "Leak rate", "Over-redaction",
                 "Probes"),
        rows=rows,
        note=note,
    )


def by_category(report: Report) -> Table:
    return _breakdown(
        report, "category", "Leak rate by category",
        "One overall number can hide a category the tool never handles, so each is "
        "shown on its own. Look for rows near 1.000.",
    )


def by_severity(report: Report) -> Table:
    return _breakdown(
        report, "severity", "Leak rate by severity",
        "Each level up counts double in the weighted leak rate, so one critical leak "
        "outweighs several low ones.",
    )


def by_difficulty(report: Report) -> Table:
    return _breakdown(
        report, "difficulty", "Leak rate by difficulty tier",
        "How the tool copes as values get harder to spot. A tool at 0.9 easy / 0.3 hard "
        "is a different product from a uniform 0.6.",
    )


def by_trap(report: Report) -> Table:
    return _breakdown(
        report, "trap", "Leak rate by structural trap",
        "Values placed to trip up tools that only read the text layer, however the page "
        "looks.",
    )


def reach(report: Report) -> Table:
    """`Reach_k`, the redaction rate over probes reachable only through channel k."""
    values = metrics.reach(report.frame, report.thresholds.z)
    rows = tuple(
        (channel, fmt_rate(rate)) for channel, rate in sorted(values.items())
    )
    return Table(
        key="reach",
        title="Channel reach",
        columns=("Channel", "Reach"),
        rows=rows,
        note="Share of values redacted that could only be found through that channel "
             "(text layer, image, metadata...). 0 means the tool does not read that "
             "channel at all. 1 is best.",
    )


def condition_cost(report: Report) -> Table:
    """The cost of each rendering condition, not its rate. Report the vector."""
    rows: list[tuple[str, ...]] = []
    for axis in ("orientation", "polarity", "provenance", "scale"):
        for level, delta in sorted(
            metrics.condition_cost(report.frame, axis).items(), key=lambda kv: -kv[1]
        ):
            rows.append((axis, level, fmt(delta)))
    return Table(
        key="condition_cost",
        title="Cost of each rendering condition",
        columns=("Axis", "Level", "Delta vs control"),
        rows=tuple(rows),
        align=(LEFT, LEFT, RIGHT),
        note="How much worse the tool does under each condition than on plain, upright "
             "text. 0 means no extra cost; 0.6 means it removes 60 points fewer values.",
    )


def layers(report: Report) -> Table:
    """How a tool leaks, which is the part a vendor can fix."""
    frame = report.frame
    rates = metrics.layer_rates(
        frame, [layer.value for layer in Layer], report.thresholds.z
    )
    rows: list[tuple[str, ...]] = []
    roles: list[tuple[str | None, ...]] = []
    clean: list[str] = []
    for layer in Layer:
        stats = rates[layer.value]
        caught = stats["layer_leak_rate"]["value"] or 0
        exclusive = stats["exclusive_leak_rate"]["value"] or 0
        if not caught and not stats["unavailable"]:
            clean.append(layer.value)
            continue
        rows.append((
            layer.value,
            layer.severity.value,
            fmt(caught),
            fmt(exclusive),
            str(stats["unavailable"]),
            layer.description,
        ))
        roles.append((None, layer.severity.value, "critical" if caught else None,
                      None, "serious" if stats["unavailable"] else None, None))
    note = ("Where in the file a leaked value was found. `Found in` is the share of "
            "sensitive values recoverable from that layer; `Only here` is the share no "
            "other layer revealed; `Unread` counts probes the layer could not be "
            "checked for.")
    if clean:
        note += f" Nothing leaked through: {', '.join(clean)}."
    return Table(
        key="layers",
        title="Where the leaks are",
        columns=("Layer", "Severity", "Found in", "Only here", "Unread",
                 "Who can see it"),
        rows=tuple(rows),
        align=(LEFT, LEFT, RIGHT, RIGHT, RIGHT, LEFT),
        roles=tuple(roles),
        note=note,
    )


def detection(report: Report) -> Table:
    """Observed mode, or the plain statement that detection is unidentifiable."""
    rows: list[tuple[str, ...]] = []
    for run in report.runs:
        found = run.report.get("detection") or {}
        label = run.run_id if len(report.runs) > 1 else "this run"
        if found.get("mode") != "observed":
            rows.append((label, "inferred", "-", "-", "-", str(found.get("note") or "")))
            continue
        rows.append((
            label, "observed",
            fmt(found.get("recall")), fmt(found.get("precision")),
            fmt(found.get("type_accuracy")),
            f"{found.get('matched')} of {found.get('reported')} reported entities matched",
        ))
    return Table(
        key="detection",
        title="Detection",
        columns=("Run", "Mode", "Recall", "Precision", "Type accuracy", "Note"),
        rows=tuple(rows),
        align=(LEFT, LEFT, RIGHT, RIGHT, RIGHT, LEFT),
        note="Only measurable when the tool reports what it found. When it does not "
             "(`inferred`), a value it never spotted and one it spotted but failed to "
             "remove look the same, so no separate detection score is given.",
    )


def ambiguous(report: Report) -> Table:
    """Arguable probes: the agreement rate, and every disagreement named."""
    rows: list[tuple[str, ...]] = []
    for run in report.runs:
        found = run.report.get("ambiguous") or {}
        for item in found.get("disagreements") or ():
            rows.append((
                str(item.get("probe_id")), str(item.get("category")),
                str(item.get("we_said")), str(item.get("tool")),
            ))
    total = sum((r.report.get("ambiguous") or {}).get("n", 0) for r in report.runs)
    agreed = sum((r.report.get("ambiguous") or {}).get("agreed", 0) for r in report.runs)
    return Table(
        key="ambiguous",
        title="Arguable calls",
        columns=("Probe", "Category", "We said", "The tool"),
        rows=tuple(rows),
        align=(LEFT, LEFT, LEFT, LEFT),
        note=f"{agreed} of {total} agreed. Values where either answer is defensible; "
             f"they are listed here and kept out of the headline numbers.",
    )


def comparison(report: Report) -> Table:
    """One pooled row per tool. Counts are summed, never rates averaged."""
    rows: list[tuple[str, ...]] = []
    roles: list[tuple[str | None, ...]] = []
    for entry in sorted(report.by_tool(), key=_rank):
        rows.append((
            entry["label"],
            fmt_rate(entry["leak_rate"], with_n=False),
            fmt_rate(entry["over_redaction_rate"], with_n=False),
            fmt(entry["text_retention"]),
            fmt(entry["rqs"]),
            f"{entry['probes']} / {entry['pages']}",
            ", ".join(entry["gates"]) or "none",
        ))
        roles.append((None, None, None, None, None, None,
                      "critical" if entry["gates"] else "good"))
    return Table(
        key="comparison",
        title="Tools compared",
        columns=("Tool", "Leak rate", "Over-redaction", "Text retention", "RQS",
                 "Probes / pages", "Failed checks"),
        rows=tuple(rows),
        align=(LEFT, RIGHT, RIGHT, RIGHT, RIGHT, RIGHT, LEFT),
        roles=tuple(roles),
        note="Best first: sorted by leak rate, then over-redaction. For the rates 0 is "
             "best; for text retention and RQS 1 is best. A tool's runs are pooled by "
             "adding up counts, never by averaging rates.",
    )


def _rank(entry: dict[str, Any]) -> tuple[float, float]:
    leak = entry["leak_rate"].value if entry["leak_rate"].defined else 2.0
    over = (entry["over_redaction_rate"].value
            if entry["over_redaction_rate"].defined else 2.0)
    return (leak, over)


def coverage(report: Report) -> Table:
    """What was not measured, and why. An exclusion must never read as a pass."""
    rows: list[tuple[str, ...]] = []
    for gap in report.coverage_gaps():
        rows.append(("tool does not attempt", gap))
    for run in report.runs:
        unsupported = run.report.get("unsupported") or {}
        for reason, count in sorted((unsupported.get("reasons") or {}).items()):
            rows.append(("unsupported probes", f"{count} x {reason}"))
    for layer, why in report.unavailable_layers().items():
        rows.append(("layer not checked", f"{layer}: {why}"))
    return Table(
        key="coverage",
        title="What was not measured",
        columns=("Kind", "Statement"),
        rows=tuple(rows),
        align=(LEFT, LEFT),
        note="Probes excluded here are not in the rates above - which is not the same "
             "as passing them. A tool that skips handwriting will leave handwritten "
             "details on a real document.",
    )


def provenance(report: Report) -> Table:
    """A score measures a tool on a date, with a stack. All of it is recorded."""
    thresholds = report.thresholds.to_dict()
    rows: list[tuple[str, ...]] = []
    engines = [dict(run.report.get("engines") or {}) for run in report.runs]
    shared = {
        engine: version for engine, version in engines[0].items()
        if version and all(e.get(engine) == version for e in engines[1:])
    } if len(report.runs) > 1 else {}
    for engine, version in shared.items():
        rows.append(("all runs", f"engine {engine}", str(version)))
    for run in report.runs:
        manifest = run.manifest
        rows.append((run.run_id, "tool", run.tool_id))
        for field_name in ("tier", "tool_version", "url", "observed_at", "operator",
                           "attempt", "transport"):
            if manifest.get(field_name):
                rows.append((run.run_id, field_name, str(manifest[field_name])))
        rows.append((run.run_id, "case", run.case_id))
        rows.append((run.run_id, "dataset revision", run.dataset_revision or "unpinned"))
        if manifest.get("output_sha256"):
            rows.append((run.run_id, "output sha256", str(manifest["output_sha256"])[:16]))
        for engine, version in (run.report.get("engines") or {}).items():
            if version and engine not in shared:
                rows.append((run.run_id, f"engine {engine}", str(version)))
        alignment = run.report.get("alignment") or {}
        rows.append((
            run.run_id, "alignment",
            f"{alignment.get('method')} · residual {alignment.get('residual_px')}px"
            f"{'' if alignment.get('confident') else ' · NOT TRUSTED'}",
        ))
    rows.extend(
        ("thresholds", key, str(value)) for key, value in sorted(thresholds.items())
    )
    return Table(
        key="provenance",
        title="Provenance",
        columns=("Run", "Field", "Value"),
        rows=tuple(rows),
        align=(LEFT, LEFT, LEFT),
        note="What produced these numbers: tool, date, dataset revision, software "
             "versions and scoring thresholds. Scores are only comparable when the "
             "dataset revision and thresholds match.",
    )


def main_tables(report: Report) -> list[Table]:
    """What most readers need, in reading order. Empty ones are dropped on render."""
    built = []
    if len(report.runs) > 1:
        built.append(comparison(report))
    built.extend([
        headline(report), counts(report), gates(report), layers(report),
        by_category(report), coverage(report),
    ])
    return built


def detail_tables(report: Report) -> list[Table]:
    """The finer breakdowns and the audit trail, for whoever needs to dig in."""
    return [
        by_severity(report), by_difficulty(report), by_trap(report), reach(report),
        condition_cost(report), detection(report), ambiguous(report),
        provenance(report),
    ]


def all_tables(report: Report) -> list[Table]:
    """Every table, in reading order."""
    return main_tables(report) + detail_tables(report)


# --- plain language -------------------------------------------------------------------


#: The terms a first-time reader trips over, in the order they meet them.
GUIDE: tuple[tuple[str, str], ...] = (
    ("Probe", "one value planted on the test page with a known right answer. Some are "
              "sensitive and must be removed (a card number, a name); some are harmless "
              "look-alikes that must stay (an invoice number)."),
    ("Leak rate", "the share of sensitive values that can still be recovered from the "
                  "output - by looking, copy-paste, a parser or the file's hidden parts. "
                  "0 is best. This is the main result."),
    ("Over-redaction", "the share of harmless values the tool removed anyway. 0 is "
                       "best. A tool that blacks out everything scores 0 leak and 1 "
                       "here, which is why both are always shown."),
    ("[low, high] n=", "`0.66 [0.58, 0.73] n=162` means 0.66 measured on 162 probes, and "
                       "the true rate is between 0.58 and 0.73 with 95% confidence. "
                       "Small n gives a wide range; compare tools by whether their "
                       "ranges overlap."),
    ("Layers", "the places inside a PDF a value can survive: what is drawn on the page, "
               "the text you can copy, metadata, attachments, older saved versions, and "
               "more. A black box drawn over text that is still copyable is a leak."),
    ("Checks", "whether the output is still a usable document. A tool that turns the "
               "page into a picture removes everything, so it looks perfect on leak rate "
               "and fails here."),
)


def _pct(numerator: int, denominator: int) -> str:
    return f"{numerator} of {denominator} ({numerator / denominator:.0%})"


def _tool_sentence(entry: dict[str, Any]) -> str:
    """One tool, one sentence, in words a non-specialist can act on."""
    leak, over = entry["leak_rate"], entry["over_redaction_rate"]
    parts = []
    if leak.defined:
        parts.append(
            "removed every sensitive value" if leak.numerator == 0
            else f"leaked {_pct(leak.numerator, leak.denominator)} sensitive values"
        )
    if over.defined:
        parts.append(
            "kept every harmless value" if over.numerator == 0
            else f"wrongly removed {_pct(over.numerator, over.denominator)} harmless ones"
        )
    text = f"{entry['label']} " + (" and ".join(parts) or "had nothing to score")
    if entry["gates"]:
        meaning = "; ".join(GATE_MEANING.get(g, g) for g in entry["gates"])
        text += f" - but {meaning}"
    return text + "."


def summary(report: Report) -> list[str]:
    """The result in a handful of sentences, before any table."""
    head = report.headline()
    lines: list[str] = []
    if len(report.runs) > 1:
        lines.extend(_tool_sentence(entry)
                     for entry in sorted(report.by_tool(), key=_rank))
    else:
        entry = report.by_tool()[0]
        lines.append(_tool_sentence(entry))
        if head["text_retention"] is not None:
            lines.append(f"{head['text_retention']:.0%} of the page's text can still be "
                         f"selected and searched.")

    split = metrics.by(report.frame, "category", report.thresholds.z)
    worst = sorted(
        (key for key, stats in split.items()
         if (stats["leak_rate"]["value"] or 0) >= 0.5),
    ) if len(report.runs) == 1 else []
    if worst:
        lines.append("Leaked at least half the time: " + ", ".join(worst) + ".")

    rates = metrics.layer_rates(
        report.frame, [layer.value for layer in Layer], report.thresholds.z
    )
    leaking = sorted(
        (layer for layer in Layer if rates[layer.value]["layer_leak_rate"]["value"]),
        key=lambda layer: -(rates[layer.value]["layer_leak_rate"]["value"] or 0),
    )
    if leaking:
        lines.append(("Across all tools, leaks" if len(report.runs) > 1 else "Leaks")
                     + " were found in: " + "; ".join(
            f"{layer.value} ({layer.description})" for layer in leaking[:3]
        ) + ".")

    skipped = report.unavailable_layers()
    if skipped:
        lines.append("Not checked: " + "; ".join(
            f"{layer} - {why}" for layer, why in skipped.items()
        ) + ". A leak there would not show up in these numbers.")
    return lines


# --- renderers ------------------------------------------------------------------------


def to_markdown(table: Table) -> str:
    if table.empty:
        return ""
    dashes = {LEFT: ":---", RIGHT: "---:"}
    out = [f"### {table.title}", ""]
    out.append("| " + " | ".join(table.columns) + " |")
    out.append("|" + "|".join(dashes[a] for a in table.alignment()) + "|")
    for row in table.rows:
        out.append("| " + " | ".join(_md_cell(cell) for cell in row) + " |")
    if table.note:
        out.extend(["", f"_{table.note}_"])
    return "\n".join(out)


def _md_cell(cell: str) -> str:
    return str(cell).replace("|", "\\|").replace("\n", " ")


def to_text(table: Table, width: int = 96) -> str:
    """Plain text for a terminal. Columns padded, no box drawing."""
    if table.empty:
        return ""
    rows = [tuple(str(c) for c in row) for row in table.rows]
    widths = [
        max(len(table.columns[i]), *(len(r[i]) for r in rows))
        for i in range(len(table.columns))
    ]
    # The last column is prose; let it use whatever is left rather than pad to it.
    widths[-1] = min(widths[-1], max(12, width - sum(widths[:-1]) - 2 * len(widths)))

    def line(cells: tuple[str, ...]) -> str:
        out = []
        for i, cell in enumerate(cells):
            text = cell if len(cell) <= widths[i] else cell[: widths[i] - 1] + "\u2026"
            out.append(text.rjust(widths[i]) if table.alignment()[i] == RIGHT
                       else text.ljust(widths[i]))
        return "  ".join(out).rstrip()

    out = [table.title, "-" * min(width, len(table.title))]
    out.append(line(table.columns))
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def probe_verdicts(head: dict[str, Any]) -> str:
    """Passed and failed probes, in one line for the top of the report."""
    text = f"{head['passed']} passed · {head['failed']} failed"
    if head["not_scored"]:
        text += f" · {head['not_scored']} not scored"
    return text


def markdown_report(report: Report, tables: list[Table] | None = None) -> str:
    """The whole report as Markdown - for an issue, a PR or a vendor email."""
    head = report.headline()
    out = [f"# {report.title}", ""]
    out.append(
        f"{head['probes']} probes on {head['pages']} page"
        f"{'s' if head['pages'] != 1 else ''}"
        f" · {len(report.runs)} run{'s' if len(report.runs) != 1 else ''}"
    )
    out.append(f"**{probe_verdicts(head)}**")
    revision = report.runs[0].dataset_revision
    if revision:
        out.append(f"Dataset revision `{revision}`.")
    out.append("")
    if tables is not None:
        for table in tables:
            rendered = to_markdown(table)
            if rendered:
                out.extend([rendered, ""])
    else:
        out.extend(["## Summary", ""])
        out.extend(f"- {line}" for line in summary(report))
        out.extend(["", "## How to read this report", ""])
        out.extend(f"- **{term}** - {text}" for term, text in GUIDE)
        out.extend(["", "## Results", ""])
        for table in main_tables(report):
            rendered = to_markdown(table)
            if rendered:
                out.extend([rendered, ""])
        out.extend(["## Details", "",
                    "_Finer breakdowns and the record of what produced the numbers. "
                    "Not needed to understand the result._", ""])
        for table in detail_tables(report):
            rendered = to_markdown(table)
            if rendered:
                out.extend([rendered, ""])
    notes = report.notes()
    if notes:
        out.extend(["### Notes", ""])
        out.extend(f"- {note}" for note in notes)
        out.append("")
    return "\n".join(out)
