"""Colours, and the rules that pick them. One place, two modes.

Every value here was run through a colour-vision validator rather than chosen by eye,
and each set records what it passed. That is not fussiness: a leak-rate chart read by a
colourblind reader who cannot separate two bars is a chart that misreports a privacy
result, and "these look different enough" is not a test.

Four jobs, four rules:

* **Categorical** - identity. Fixed slot order, never cycled.
* **Sequential / ordinal** - magnitude. One hue, light to dark.
* **Diverging** - polarity. Two opposed hues with a neutral grey midpoint, for numbers
  that can sit either side of a baseline.
* **Status** - state. Reserved: never reused as a series colour, and always shipped
  with a glyph and a label so the colour never carries the meaning alone.

Dark mode is a second *selected* set stepped for the dark surface, not an automatic
inversion of the light one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Mode:
    """Every colour one render mode needs."""

    name: str
    surface: str
    plane: str
    ink: str
    ink_secondary: str
    ink_muted: str
    grid: str
    axis: str
    border: str
    #: Categorical slot 1. Single-series charts use this and nothing else: a value ramp
    #: over nominal categories would double-encode bar length as hue.
    series: str
    #: Two steps of the series hue, for a part-to-whole bar. Validated `--ordinal`:
    #: monotone lightness, a visible gap between steps, light end clear of the surface.
    ramp_strong: str
    ramp_weak: str
    #: Diverging poles - warm against cool, so they read as opposite - and the neutral
    #: midpoint, which has to read as "nothing".
    diverge_low: str
    diverge_high: str
    diverge_mid: str

    def figure_kwargs(self) -> dict[str, object]:
        """Matplotlib wants the surface transparent so the card behind shows through."""
        return {"facecolor": "none", "edgecolor": "none"}


#: Light. Surface #fcfcfb. Categorical slot 1 and the diverging pair both clear 3:1
#: against it; the two-step ramp passes the ordinal gate (light end 2.06:1).
LIGHT = Mode(
    name="light",
    surface="#fcfcfb",
    plane="#f9f9f7",
    ink="#0b0b0b",
    ink_secondary="#52514e",
    ink_muted="#898781",
    grid="#e1e0d9",
    axis="#c3c2b7",
    border="rgba(11,11,11,0.10)",
    series="#2a78d6",
    ramp_strong="#1c5cab",
    ramp_weak="#86b6ef",
    diverge_low="#2a78d6",
    diverge_high="#e34948",
    diverge_mid="#f0efec",
)

#: Dark. Surface #1a1a19. The same hues re-stepped for the dark band and validated as
#: their own set - the light steps would sit far too close to the surface.
DARK = Mode(
    name="dark",
    surface="#1a1a19",
    plane="#0d0d0d",
    ink="#ffffff",
    ink_secondary="#c3c2b7",
    ink_muted="#898781",
    grid="#2c2c2a",
    axis="#383835",
    border="rgba(255,255,255,0.10)",
    series="#3987e5",
    ramp_strong="#184f95",
    ramp_weak="#9ec5f4",
    diverge_low="#3987e5",
    diverge_high="#e66767",
    diverge_mid="#383835",
)

MODES = (LIGHT, DARK)

#: Reserved for state, identical in both modes, and never a series colour. Each is
#: published with the glyph beside it, because two of them are below 3:1 on the light
#: surface by design and colour must not be the only channel.
STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

#: Glyph per outcome, so an overlay box and a table cell both say what they mean
#: without relying on the colour being seen.
OUTCOME_STYLE = {
    "FN": ("critical", "!", "leak"),
    "TP": ("good", "*", "redacted"),
    "FP": ("warning", "+", "over-redacted"),
    "TN": ("neutral", ".", "kept"),
    "unsupported": ("neutral", "-", "out of scope"),
    "undecided": ("serious", "?", "undecided"),
}


def outcome_colour(outcome: str, mode: Mode) -> str:
    """The colour for a probe outcome. Neutral outcomes wear ink, not a status hue."""
    role, _, _ = OUTCOME_STYLE.get(outcome, ("neutral", "?", outcome))
    return mode.ink_muted if role == "neutral" else STATUS[role]


def outcome_glyph(outcome: str) -> str:
    return OUTCOME_STYLE.get(outcome, ("neutral", "?", outcome))[1]


def outcome_label(outcome: str) -> str:
    return OUTCOME_STYLE.get(outcome, ("neutral", "?", outcome))[2]


#: The page's type. One sans throughout, including the hero figure: a display face on a
#: number reads as decoration, and this document is evidence.
FONT_STACK = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
