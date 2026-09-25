"""One self-contained HTML file: the report as something you can send someone.

Self-contained is the requirement, not a nicety. A benchmark result gets forwarded,
attached to a ticket and opened months later by someone arguing with it; a page that
needs a CDN, a font server or a sibling `charts/` directory is a page that will one day
render as broken boxes. Everything - styles, SVG, images - is inlined.

The page leads with the leak rate as a hero figure, because that is the number the
result *is*. Both axes are always shown, leak rate first: an under-redaction discloses
data, an over-redaction only wastes a page.

Dark mode is a second set of steps, selected and validated for the dark surface, not an
inverted light one - so every chart is rendered twice and CSS picks.
"""

from __future__ import annotations

import base64
import html as _html
from pathlib import Path
from typing import Any

from . import charts, tables
from .model import Report
from .palette import DARK, FONT_STACK, LIGHT, STATUS, Mode
from .tables import Table, fmt


def _esc(text: Any) -> str:
    return _html.escape(str(text), quote=True)


def _vars(mode: Mode, scope: str) -> str:
    return f"""{scope} {{
    color-scheme: {mode.name};
    --surface: {mode.surface};
    --plane: {mode.plane};
    --ink: {mode.ink};
    --ink-2: {mode.ink_secondary};
    --ink-muted: {mode.ink_muted};
    --grid: {mode.grid};
    --axis: {mode.axis};
    --border: {mode.border};
    --series: {mode.series};
    --good: {STATUS['good']};
    --warning: {STATUS['warning']};
    --serious: {STATUS['serious']};
    --critical: {STATUS['critical']};
  }}"""


def _styles() -> str:
    return f"""<style>
  {_vars(LIGHT, ":root")}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) {{
      color-scheme: dark;
      --surface: {DARK.surface}; --plane: {DARK.plane}; --ink: {DARK.ink};
      --ink-2: {DARK.ink_secondary}; --ink-muted: {DARK.ink_muted};
      --grid: {DARK.grid}; --axis: {DARK.axis}; --border: {DARK.border};
      --series: {DARK.series};
    }}
    :root:where(:not([data-theme="light"])) .only-light {{ display: none; }}
    :root:where(:not([data-theme="light"])) .only-dark {{ display: block; }}
  }}
  :root[data-theme="dark"] {{
    color-scheme: dark;
    --surface: {DARK.surface}; --plane: {DARK.plane}; --ink: {DARK.ink};
    --ink-2: {DARK.ink_secondary}; --ink-muted: {DARK.ink_muted};
    --grid: {DARK.grid}; --axis: {DARK.axis}; --border: {DARK.border};
    --series: {DARK.series};
  }}
  :root[data-theme="dark"] .only-light {{ display: none; }}
  :root[data-theme="dark"] .only-dark {{ display: block; }}
  .only-dark {{ display: none; }}

  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--plane); color: var(--ink);
    font-family: {FONT_STACK}; font-size: 15px; line-height: 1.55;
    -webkit-text-size-adjust: 100%;
  }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 32px 16px 80px; }}
  header {{ margin-bottom: 28px; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 4px; letter-spacing: -0.01em; }}
  .sub {{ color: var(--ink-2); font-size: 0.9rem; }}
  .card {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 20px; margin: 18px 0;
  }}
  h2 {{ font-size: 1.05rem; margin: 0 0 2px; }}
  h3 {{ font-size: 0.95rem; margin: 0 0 2px; }}
  .note {{ color: var(--ink-2); font-size: 0.85rem; margin: 2px 0 14px; max-width: 68ch;
    overflow-wrap: anywhere; }}

  /* `min-width: 0` is load-bearing: without it a flex child refuses to shrink below
     its content, the card grows wider than the phone, and every row on the page is
     clipped at the right edge. */
  .hero {{ display: flex; flex-wrap: wrap; gap: 28px; align-items: flex-end; }}
  .hero > * {{ min-width: 0; }}
  .hero .caption {{ max-width: 62ch; }}
  .hero .figure {{ font-size: 3.4rem; line-height: 1; font-weight: 600; }}
  .hero .caption {{ color: var(--ink-2); font-size: 0.85rem; margin-top: 6px; }}
  .kpis {{
    display: grid; gap: 12px; margin-top: 20px;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  }}
  .kpi {{ border-top: 2px solid var(--grid); padding-top: 8px; }}
  .kpi .label {{ color: var(--ink-2); font-size: 0.8rem; }}
  .kpi .value {{ font-size: 1.35rem; font-weight: 600; }}
  .kpi .hint {{ color: var(--ink-muted); font-size: 0.78rem; }}

  .flags {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }}
  .flag {{
    font-size: 0.8rem; padding: 3px 9px; border-radius: 999px;
    border: 1px solid var(--border); color: var(--ink-2);
  }}
  .verdicts {{ display: inline-flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }}
  .verdicts .flag {{ color: var(--ink); font-weight: 600; }}
  .flag .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 6px; vertical-align: 1px; }}

  figure {{ margin: 0; }}
  figure svg {{ max-width: 100%; height: auto; display: block; }}
  .shot {{ width: 100%; height: auto; border: 1px solid var(--border);
    border-radius: 6px; }}

  /* A wide table scrolls itself rather than the page. */
  .scroll {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.87rem;
    font-variant-numeric: tabular-nums; }}
  th, td {{ padding: 7px 10px; border-bottom: 1px solid var(--grid);
    text-align: left; vertical-align: top; }}
  th {{ color: var(--ink-2); font-weight: 600; white-space: nowrap; }}
  td.right, th.right {{ text-align: right; }}
  tbody tr:last-child td {{ border-bottom: none; }}
  .glyph {{ font-weight: 700; margin-right: 5px; }}

  ul.notes {{ padding-left: 18px; color: var(--ink-2); font-size: 0.87rem; }}
  ul.summary {{ padding-left: 18px; margin: 8px 0 0; }}
  ul.summary li {{ margin: 4px 0; }}
  dl.guide {{ display: grid; grid-template-columns: max-content 1fr; gap: 6px 16px;
    margin: 10px 0 0; font-size: 0.88rem; }}
  dl.guide dt {{ font-weight: 600; }}
  dl.guide dd {{ margin: 0; color: var(--ink-2); }}
  h2.section {{ font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--ink-muted); margin: 34px 0 0; }}
  details.more > summary {{ cursor: pointer; font-weight: 600; margin: 34px 0 0;
    color: var(--ink-2); }}
  code {{ font-size: 0.9em; }}
  footer {{ color: var(--ink-muted); font-size: 0.8rem; margin-top: 36px; }}
  a {{ color: var(--series); }}
  @media (max-width: 620px) {{
    .wrap {{ padding: 24px 16px 60px; }}
    .hero .figure {{ font-size: 2.6rem; }}
    th, td {{ padding: 6px 7px; }}
    dl.guide {{ grid-template-columns: 1fr; gap: 2px; }}
    dl.guide dd {{ margin-bottom: 8px; }}
  }}
</style>"""


def _status_dot(role: str | None) -> str:
    if not role or role not in STATUS:
        return ""
    glyph = {"good": "✓", "warning": "+", "serious": "?", "critical": "!"}[role]
    return (f'<span class="glyph" style="color: var(--{role})" aria-hidden="true">'
            f'{glyph}</span>')


def table_html(table: Table) -> str:
    """A table, with the status glyph beside the colour rather than instead of it."""
    if table.empty:
        return ""
    align = table.alignment()
    head = "".join(
        f'<th class="{"right" if a == tables.RIGHT else ""}">{_esc(c)}</th>'
        for c, a in zip(table.columns, align, strict=True)
    )
    body = []
    for index, row in enumerate(table.rows):
        roles = table.roles[index] if index < len(table.roles) else ()
        cells = []
        for i, cell in enumerate(row):
            role = roles[i] if i < len(roles) else None
            cells.append(
                f'<td class="{"right" if align[i] == tables.RIGHT else ""}">'
                f"{_status_dot(role)}{_esc(cell)}</td>"
            )
        body.append("<tr>" + "".join(cells) + "</tr>")
    note = f'<p class="note">{_esc(table.note)}</p>' if table.note else ""
    return (
        f'<section class="card"><h2>{_esc(table.title)}</h2>{note}'
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
        f"</section>"
    )


def _verdicts(head: dict[str, Any]) -> str:
    """Passed and failed probe counts. The words carry the meaning; the dot only helps."""
    parts = [
        f'<span class="flag"><span class="dot" style="background: var(--good)"></span>'
        f'{head["passed"]} passed</span>',
        f'<span class="flag"><span class="dot" style="background: var(--critical)">'
        f'</span>{head["failed"]} failed</span>',
    ]
    if head["not_scored"]:
        parts.append(f'<span class="flag">{head["not_scored"]} not scored</span>')
    return " ".join(parts)


def _hero(report: Report) -> str:
    head = report.headline()
    leak = head["leak_rate"]
    value = fmt(leak.value) if leak.defined else "n/a"
    interval = (f"[{leak.low:.2f}, {leak.high:.2f}] over n={leak.denominator}"
                if leak.defined else "no scored targets")

    # The value is the number; the interval rides in the hint. A tile whose value wraps
    # onto a second line throws the whole row out of alignment.
    over = head["over_redaction_rate"]
    over_hint = (
        f"[{over.low:.2f}, {over.high:.2f}] n={over.denominator}"
        if over.defined else "no distractors on this page"
    )
    kpis = [
        ("Over-redaction", fmt(over.value) if over.defined else "n/a",
         f"harmless values removed · 0 is best · {over_hint}"),
        ("Weighted leak rate", fmt(head["weighted_leak_rate"]),
         "severe leaks count more · 0 is best"),
        ("Text retention", fmt(head["text_retention"]),
         "text still selectable · 1 is best"),
        ("Area over-coverage", fmt(head["area_over_coverage"], 4),
         "page blacked out needlessly · 0 is best"),
        ("RQS (beta=2)", fmt(head["rqs"]), "for sorting only · 1 is best"),
    ]
    tiles = "".join(
        f'<div class="kpi"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{_esc(value_)}</div>'
        f'<div class="hint">{_esc(hint)}</div></div>'
        for label, value_, hint in kpis
    )

    failures = sorted({g for run in report.runs for g in run.gate_failures})
    flags = "".join(
        f'<span class="flag"><span class="dot" style="background: var(--critical)">'
        f"</span>gate failed: {_esc(gate)} &middot; "
        f"{_esc(tables.GATE_MEANING.get(gate, ''))}</span>"
        for gate in failures
    ) or (
        '<span class="flag"><span class="dot" style="background: var(--good)"></span>'
        "document still usable: all checks pass</span>"
    )
    return (
        f'<section class="card"><div class="hero">'
        f'<div><div class="figure">{_esc(value)}</div>'
        f'<div class="caption">leak rate &middot; {_esc(interval)}</div></div>'
        f'<div class="caption">{head["probes"]} probes on {head["pages"]} page'
        f'{"s" if head["pages"] != 1 else ""} &middot; '
        f'{len(report.runs)} run{"s" if len(report.runs) != 1 else ""}<br>'
        f'<span class="verdicts">{_verdicts(head)}</span><br>'
        f"Leak rate is the share of sensitive values still recoverable from the "
        f"output. 0 is best.</div></div>"
        f'<div class="kpis">{tiles}</div><div class="flags">{flags}</div></section>'
    )


def _summary(report: Report) -> str:
    items = "".join(f"<li>{_esc(line)}</li>" for line in tables.summary(report))
    return (f'<section class="card"><h2>Summary</h2>'
            f'<ul class="summary">{items}</ul></section>')


def _guide() -> str:
    """The glossary, for someone opening a report like this for the first time."""
    rows = "".join(
        f"<dt>{_esc(term)}</dt><dd>{_esc(text)}</dd>" for term, text in tables.GUIDE
    )
    return (f'<section class="card"><h2>How to read this report</h2>'
            f'<dl class="guide">{rows}</dl></section>')


def _figure(title: str, note: str, light: Any, dark: Any) -> str:
    """A chart card, or - when there is nothing to plot - the reason, in a sentence."""
    if light.svg is None:
        return (
            f'<section class="card"><h2>{_esc(title)}</h2>'
            f'<p class="note">{_esc(light.reason)}</p></section>'
        )
    return (
        f'<section class="card"><h2>{_esc(title)}</h2>'
        f'<p class="note">{_esc(note)}</p>'
        f'<figure><div class="only-light">{light.svg}</div>'
        f'<div class="only-dark">{dark.svg}</div></figure></section>'
    )


def _image_card(title: str, note: str, path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return (
        f'<section class="card"><h2>{_esc(title)}</h2>'
        f'<p class="note">{_esc(note)}</p>'
        f'<img class="shot" alt="{_esc(title)}" src="data:image/png;base64,{data}">'
        f"</section>"
    )


#: What each chart is for, printed beside it. A chart with no sentence is decoration.
CHART_NOTES = {
    "privacy_utility": (
        "Each dot is a tool. Higher means fewer leaks; further right means fewer "
        "harmless values destroyed. The shaded corner is where a tool worth using sits. "
        "Top-left is blacking out everything (nothing leaks, nothing useful is left); "
        "bottom-right is doing nothing."
    ),
    "leak_layers": (
        "Where in the file leaked values were found - each layer is a different way "
        "someone could recover them. The dark part of a bar is what only that layer "
        "revealed. An empty chart is the good outcome."
    ),
    "leak_by_category": (
        "Leak rate for each kind of sensitive value; shorter is better. The whisker is "
        "the 95% range - a wide one means few probes, so read it loosely."
    ),
    "reach": (
        "Share of values redacted that could only be found through each channel. A "
        "channel at zero means the tool does not read it at all."
    ),
    "condition_cost": (
        "How much worse the tool does under each condition than on plain, upright "
        "text. A bar near zero means that condition costs nothing."
    ),
}

#: Charts shown in the main flow; the others' numbers are in the details tables.
MAIN_CHARTS = ("leak_layers", "leak_by_category")


def render(
    report: Report,
    *,
    images: list[Path] | None = None,
    image_notes: dict[str, str] | None = None,
) -> str:
    """The whole report as one HTML document."""
    light = charts.render_all(report, LIGHT)
    dark = charts.render_all(report, DARK)

    revision = report.runs[0].dataset_revision
    observed = report.runs[0].observed_at
    subtitle = " &middot; ".join(filter(None, [
        f"dataset revision <code>{_esc(revision)}</code>" if revision else
        "dataset revision unpinned",
        f"observed {_esc(observed)}" if observed else None,
    ]))

    def chart(key: str) -> str:
        title = charts.CHARTS[key][0]
        return _figure(title, CHART_NOTES.get(key, ""), light[key], dark[key])

    body = [_hero(report), _summary(report), _guide()]
    body.append('<h2 class="section">Results</h2>')
    if len(report.runs) > 1:
        body.append(table_html(tables.comparison(report)))
        body.append(chart("privacy_utility"))
    body.append(table_html(tables.headline(report)))
    body.append(table_html(tables.counts(report)))
    body.append(table_html(tables.gates(report)))
    body.extend(chart(key) for key in MAIN_CHARTS)
    body.append(table_html(tables.coverage(report)))

    notes = image_notes or {}
    for path in images or ():
        default = (
            "Ground truth drawn over the tool's own output, aligned back to the "
            "original page. A box is labelled with its probe id and an outcome glyph, "
            "so the colour is never carrying the meaning alone."
        )
        body.append(_image_card(
            f"{path.stem.rsplit('-', 1)[0]} - overlay", notes.get(path.name, default),
            path,
        ))

    details = [table_html(tables.layers(report)), table_html(tables.by_category(report))]
    details.extend(table_html(table) for table in tables.detail_tables(report))
    body.append(
        '<details class="more"><summary>Details - exact numbers behind the charts, '
        "finer breakdowns and what produced this report</summary>"
        + "".join(details) + "</details>"
    )

    lines = report.notes()
    if lines:
        items = "".join(f"<li>{_esc(line)}</li>" for line in lines)
        body.append(
            f'<section class="card"><h2>Notes</h2><ul class="notes">{items}</ul>'
            f"</section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(report.title)}</title>
{_styles()}
</head>
<body>
<div class="wrap">
<header>
  <h1>{_esc(report.title)}</h1>
  <div class="sub">{subtitle}</div>
</header>
{"".join(body)}
<footer>
  Generated by pdfredeval. Every rate is shown with its 95% range and the number of
  probes behind it. A score describes one tool version on one date; the Provenance table
  under Details records exactly what produced it.
</footer>
</div>
</body>
</html>
"""
