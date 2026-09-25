"""Charts, as SVG strings. Matplotlib with the chrome turned down.

Four rules run through every figure here, and each of them is a mistake this file is
deliberately not making:

* **One hue for a single series.** Shading nominal categories darker-where-bigger
  double-encodes bar length as colour and spends the only free channel on information
  the bar already carries.
* **Never two y-scales.** Two measures of different scale get two charts.
* **A value is never colour-only.** Every bar is direct-labelled at its tip and every
  chart has a table beside it in the report, so nothing is gated behind a hue.
* **An interval is part of the number.** Rates carry Wilson whiskers, because a `0.0`
  over four probes and a `0.0` over four hundred are different claims.

Figures are transparent and drawn once per mode, so the page's surface shows through
and dark mode is a *selected* set of steps rather than an inverted light one. Output is
SVG with live text: it stays selectable, searchable and crisp, and it diffs.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

from ..probe.base import Layer
from ..score import metrics
from .model import Report
from .palette import FONT_STACK, Mode


@dataclass(frozen=True, slots=True)
class Figure:
    """A chart, or the reason there is nothing to draw.

    An empty plot box is worse than a sentence: the reader cannot tell "no data" from
    "nothing happened", and those are different findings.
    """

    svg: str | None = None
    reason: str = ""


#: Bar thickness is capped so a band's leftover stays as air. In figure terms that is a
#: fixed height per row plus the axis band.
ROW_HEIGHT = 0.34
BAR_HEIGHT = 0.5
MAX_ROWS = 14

#: Matplotlib's self-describing RDF block, which an inlined figure does not need.
_METADATA = re.compile(r"<metadata>.*?</metadata>", re.S)

#: Rate axes all carry the same ticks, so two charts in one report read alike.
RATE_TICKS = ([0, 0.25, 0.5, 0.75, 1.0], ["0", ".25", ".5", ".75", "1"])

#: Where a colliding scatter label may move to, in order: stay put, then alternate
#: down and up in small steps.
_LABEL_OFFSETS = (0.0, -0.06, 0.06, -0.12, 0.12, -0.18, 0.18, -0.24, 0.24)


def _pyplot() -> Any:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Live text in the SVG, no timestamp, and a fixed hash salt so the clip-path ids
    # are derived rather than random: the same numbers then produce the same bytes,
    # which is the property the rest of this project is built on, and it means a
    # regenerated report diffs to nothing when nothing changed.
    matplotlib.rcParams.update({
        "svg.fonttype": "none",
        "svg.hashsalt": "pdfredeval",
        "font.family": "sans-serif",
        "font.sans-serif": ["system-ui", "-apple-system", "Segoe UI", "Roboto",
                            "DejaVu Sans"],
        "figure.autolayout": False,
        "path.simplify": True,
    })
    return plt


def _style(axes: Any, mode: Mode, *, xlabel: str = "") -> None:
    """Recessive chrome: hairline grid, no box, muted ticks, solid never dashed."""
    axes.set_facecolor("none")
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(mode.axis)
    axes.spines["bottom"].set_linewidth(0.8)
    axes.tick_params(colors=mode.ink_muted, labelsize=8.5, length=0)
    axes.grid(axis="x", color=mode.grid, linewidth=0.8, linestyle="-", zorder=0)
    axes.set_axisbelow(True)
    if xlabel:
        axes.set_xlabel(xlabel, color=mode.ink_secondary, fontsize=8.5)
    for label in axes.get_yticklabels() + axes.get_xticklabels():
        label.set_color(mode.ink_secondary)


def _render(figure: Any) -> Figure:
    buffer = io.StringIO()
    figure.savefig(
        buffer, format="svg", transparent=True, bbox_inches="tight", pad_inches=0.08,
        metadata={"Date": None},
    )
    import matplotlib.pyplot as plt

    plt.close(figure)
    svg = buffer.getvalue()
    # Drop the XML prologue and the DOCTYPE: the SVG is inlined into an HTML page, and
    # a second prologue mid-document is invalid.
    start = svg.find("<svg")
    svg = svg[start:] if start >= 0 else svg
    # Matplotlib ships an RDF block naming itself and half a dozen namespace URLs. In a
    # standalone file that is provenance; inlined into a page it is several hundred
    # bytes of noise per figure, and the only thing in the document that looks like an
    # external reference.
    svg = _METADATA.sub("", svg)
    return Figure(svg.replace("font-family: sans-serif", f"font-family: {FONT_STACK}"))


def _empty(reason: str) -> Figure:
    return Figure(None, reason)


# --- the charts -----------------------------------------------------------------------


def leak_by_category(report: Report, mode: Mode) -> Figure:
    """Horizontal bars, one hue, Wilson whiskers, value at the tip.

    Horizontal because category names are long, and sorted worst-first because the
    reader's question is "where does this tool fail", not "what is the alphabet".
    """
    split = metrics.by(report.frame, "category", report.thresholds.z)
    entries = [
        (key, stats["leak_rate"]) for key, stats in split.items()
        if stats["leak_rate"]["value"] is not None
    ]
    if not entries:
        return _empty("No scored targets to break down by category.")
    entries.sort(key=lambda kv: (-(kv[1]["value"] or 0), kv[0]))
    entries = entries[:MAX_ROWS]

    plt = _pyplot()
    names = [key for key, _ in entries]
    values = [rate["value"] for _, rate in entries]
    lows = [v - (rate["ci95"][0]) for v, (_, rate) in zip(values, entries, strict=True)]
    highs = [(rate["ci95"][1]) - v for v, (_, rate) in zip(values, entries, strict=True)]

    figure, axes = plt.subplots(figsize=(6.4, 0.6 + ROW_HEIGHT * len(entries)))
    positions = range(len(entries))
    axes.barh(list(positions), values, height=BAR_HEIGHT, color=mode.series,
              zorder=2, linewidth=0)
    axes.errorbar(values, list(positions), xerr=[lows, highs], fmt="none",
                  ecolor=mode.ink_muted, elinewidth=1.0, capsize=3, zorder=3)
    for y, (name, rate) in enumerate(entries):
        axes.text(min(1.0, (rate["ci95"][1])) + 0.02, y,
                  f"{rate['value']:.2f}  n={rate['n']}",
                  va="center", fontsize=8.5, color=mode.ink_secondary)
        _ = name
    axes.set_yticks(list(positions), names)
    axes.set_xlim(0, 1.32)
    axes.set_xticks(*RATE_TICKS)
    axes.invert_yaxis()
    _style(axes, mode, xlabel="leak rate (0 is best) · bars show the 95% interval")
    return _render(figure)


def leak_layers(report: Report, mode: Mode) -> Figure:
    """Per layer: how often it catches a leak, and how often only it does.

    A stacked bar, because the exclusive share is part of the caught share - two steps
    of one hue with a surface gap between them, not a border.
    """
    rates = metrics.layer_rates(
        report.frame, [layer.value for layer in Layer], report.thresholds.z
    )
    entries = [
        (layer.value,
         rates[layer.value]["layer_leak_rate"]["value"] or 0.0,
         rates[layer.value]["exclusive_leak_rate"]["value"] or 0.0)
        for layer in Layer
    ]
    entries = [e for e in entries if e[1] > 0]
    if not entries:
        return _empty("No layer caught a leak in this run.")
    entries.sort(key=lambda e: -e[1])

    plt = _pyplot()
    names = [name for name, _, _ in entries]
    caught = [c for _, c, _ in entries]
    exclusive = [e for _, _, e in entries]
    shared = [max(0.0, c - e) for c, e in zip(caught, exclusive, strict=True)]

    figure, axes = plt.subplots(figsize=(6.4, 0.7 + ROW_HEIGHT * len(entries)))
    positions = list(range(len(entries)))
    axes.barh(positions, exclusive, height=BAR_HEIGHT, color=mode.ramp_strong,
              label="only this layer catches it", zorder=2, linewidth=0)
    # The 2px surface gap between the two segments: white doing the separating, rather
    # than a stroke that would add ink the data did not earn.
    axes.barh(positions, shared, left=exclusive, height=BAR_HEIGHT,
              color=mode.ramp_weak, label="also caught elsewhere", zorder=2,
              linewidth=1.6, edgecolor=mode.surface)
    for y, value in enumerate(caught):
        axes.text(value + 0.02, y, f"{value:.2f}", va="center", fontsize=8.5,
                  color=mode.ink_secondary)
    axes.set_yticks(positions, names)
    axes.set_xlim(0, 1.18)
    axes.set_xticks(*RATE_TICKS)
    axes.invert_yaxis()
    _style(axes, mode, xlabel="share of targets this layer catches")
    legend = axes.legend(loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=2,
                         frameon=False, fontsize=8.5, handlelength=1.2,
                         handleheight=0.9, borderaxespad=0.4)
    for text in legend.get_texts():
        text.set_color(mode.ink_secondary)
    return _render(figure)


def condition_cost(report: Report, mode: Mode) -> Figure:
    """Delta against the control, per rendering condition. Diverging around zero.

    Polarity is real here: a positive delta is a condition the tool loses ground on, a
    negative one is a condition it handles better than the control. So the two arms get
    opposed hues and the midpoint stays neutral.
    """
    entries: list[tuple[str, float]] = []
    for axis in ("orientation", "polarity", "provenance", "scale"):
        control_free = {
            level: delta for level, delta in metrics.condition_cost(
                report.frame, axis
            ).items() if abs(delta) > 1e-9
        }
        entries.extend((f"{level}", delta) for level, delta in control_free.items())
    if not entries:
        return _empty(
            "Every probe on this page is the control cell, so there is no condition "
            "to cost. The condition matrix is its own case family."
        )
    entries.sort(key=lambda kv: -kv[1])
    entries = entries[:MAX_ROWS]

    plt = _pyplot()
    names = [name for name, _ in entries]
    values = [delta for _, delta in entries]
    colours = [mode.diverge_high if v > 0 else mode.diverge_low for v in values]

    figure, axes = plt.subplots(figsize=(6.4, 0.7 + ROW_HEIGHT * len(entries)))
    positions = list(range(len(entries)))
    axes.barh(positions, values, height=BAR_HEIGHT, color=colours, zorder=2,
              linewidth=0)
    for y, value in enumerate(values):
        offset = 0.02 if value >= 0 else -0.02
        axes.text(value + offset, y, f"{value:+.2f}", va="center",
                  ha="left" if value >= 0 else "right", fontsize=8.5,
                  color=mode.ink_secondary)
    axes.axvline(0, color=mode.axis, linewidth=1.0, zorder=3)
    axes.set_yticks(positions, names)
    span = max(0.25, max(abs(v) for v in values) * 1.45)
    axes.set_xlim(-span, span)
    axes.invert_yaxis()
    _style(axes, mode, xlabel="reach lost against the control (right is worse)")
    return _render(figure)


def reach(report: Report, mode: Mode) -> Figure:
    """Redaction rate per extraction channel. A floor on what the tool read at all."""
    values = metrics.reach(report.frame, report.thresholds.z)
    entries = [(k, v) for k, v in values.items() if v["value"] is not None]
    if not entries:
        return _empty("No channel carried a scored target.")
    entries.sort(key=lambda kv: kv[1]["value"] or 0)

    plt = _pyplot()
    figure, axes = plt.subplots(figsize=(6.4, 0.7 + ROW_HEIGHT * len(entries)))
    positions = list(range(len(entries)))
    axes.barh(positions, [v["value"] for _, v in entries], height=BAR_HEIGHT,
              color=mode.series, zorder=2, linewidth=0)
    for y, (_, rate) in enumerate(entries):
        axes.text((rate["value"] or 0) + 0.02, y, f"{rate['value']:.2f}  n={rate['n']}",
                  va="center", fontsize=8.5, color=mode.ink_secondary)
    axes.set_yticks(positions, [k for k, _ in entries])
    axes.set_xlim(0, 1.32)
    axes.set_xticks(*RATE_TICKS)
    _style(axes, mode, xlabel="reach (1 is best - the tool reached every probe)")
    return _render(figure)


def privacy_utility(report: Report, mode: Mode) -> Figure:
    """The two axes, with the degenerate corners named.

    One hue for every point: identity is carried by the direct label, which keeps the
    chart honest however many tools are on it. Blacking out the page scores perfectly on
    privacy; returning the input scores perfectly on utility; the target is the corner
    where both hold.
    """
    entries = [
        e for e in report.by_tool()
        if e["privacy"] == e["privacy"] and e["utility"] == e["utility"]
    ]
    if not entries:
        return _empty(
            "Both axes need a scored target and a scored distractor; this run has "
            "only one of the two populations."
        )

    plt = _pyplot()
    figure, axes = plt.subplots(figsize=(6.4, 4.6))
    axes.add_patch(plt.Rectangle(
        (0.8, 0.8), 0.2, 0.2, facecolor=mode.series, alpha=0.08, zorder=1, linewidth=0,
    ))
    # Inside the box at its lower-left: the top-right corner is where a good tool
    # plots, and a caption there would be the first thing a point label collided with.
    axes.text(0.815, 0.815, "target", ha="left", va="bottom", fontsize=8.5,
              color=mode.ink_muted)

    # Two tools that both do nothing land on the same spot. Stacking their labels
    # would detach each from its point and read as noise, so a displaced label keeps a
    # thin leader back to the dot it belongs to.
    placed: list[tuple[float, float]] = []
    for entry in sorted(entries, key=lambda e: (-e["privacy"], -e["utility"])):
        x, y = float(entry["utility"]), float(entry["privacy"])
        axes.scatter([x], [y], s=90, color=mode.series, edgecolors=mode.surface,
                     linewidths=2, zorder=4)
        # Try the point's own height first, then step away from it alternately down
        # and up - so a label never has to leave the axes to find room.
        label_y = y
        for step in _LABEL_OFFSETS:
            candidate = y + step
            if not 0.0 <= candidate <= 1.0:
                continue
            if not any(abs(candidate - py) < 0.06 and abs(x - px) < 0.35
                       for px, py in placed):
                label_y = candidate
                break
        placed.append((x, label_y))

        # Labels on the right half point inwards, so a long tool id never runs off the
        # plot and gets clipped.
        inward = x > 0.6
        offset = -0.025 if inward else 0.025
        axes.annotate(
            entry["label"], xy=(x, y), xytext=(x + offset, label_y),
            fontsize=8.5, color=mode.ink_secondary, va="center",
            ha="right" if inward else "left",
            arrowprops=(
                {"arrowstyle": "-", "linewidth": 0.8, "color": mode.axis,
                 "shrinkA": 2, "shrinkB": 6}
                if abs(label_y - y) > 1e-9 else None
            ),
        )
    axes.set_xlim(-0.02, 1.08)
    axes.set_ylim(-0.02, 1.06)
    axes.set_xticks([0, 0.5, 1.0], ["0", ".5", "1"])
    axes.set_yticks([0, 0.5, 1.0], ["0", ".5", "1"])
    axes.set_ylabel("privacy  (1 - wLR)", color=mode.ink_secondary, fontsize=8.5)
    _style(axes, mode, xlabel="utility  (1 - ORR)")
    axes.grid(axis="y", color=mode.grid, linewidth=0.8, linestyle="-", zorder=0)
    return _render(figure)


#: Chart key -> (title, builder). The HTML page walks this in order.
CHARTS = {
    "privacy_utility": ("Privacy against utility", privacy_utility),
    "leak_layers": ("How this tool leaks", leak_layers),
    "leak_by_category": ("Leak rate by category", leak_by_category),
    "reach": ("Channel reach", reach),
    "condition_cost": ("Cost of each rendering condition", condition_cost),
}


def render_all(report: Report, mode: Mode) -> dict[str, Figure]:
    """Every chart for one mode, keyed as in `CHARTS`."""
    return {key: build(report, mode) for key, (_, build) in CHARTS.items()}
