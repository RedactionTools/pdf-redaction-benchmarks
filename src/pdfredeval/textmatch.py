"""Normalisation and the residual-disclosure test. Per docs/metrics/core.md.

The same code answers two questions at opposite ends of the pipeline, which is why it
lives here rather than inside the scorer:

* *Generation*: no two probes on a page may share an identifying token, or a correct
  redaction of one would score as a leak of the other (`generate/values.py`).
* *Scoring*: did any disclosure unit of this probe survive into the output?

If those two used different tests, the generator could certify a page the scorer then
mis-scores. Sharing the test makes that impossible by construction.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

#: Characters that carry no information but break a naive substring match. Soft hyphens
#: and the zero-width family are inserted by layout engines and survive extraction.
_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍⁠﻿"), None)

#: NFKC misses these: they are not compatibility characters, but every extractor that
#: can spell them out does.
_EXPAND = {
    "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe",
    "ß": "ss", "ı": "i", "Ł": "L", "ł": "l",
    "Ø": "O", "ø": "o", "Ð": "D", "ð": "d",
    "Þ": "Th", "þ": "th",
}


def normalize(text: str) -> str:
    """`N()`: NFKC, ligatures expanded, casefolded, invisibles gone, spaces collapsed.

    Applied to both sides of every comparison, so a tool is never credited with a
    redaction it achieved only by changing case or re-encoding a ligature.
    """
    if not text:
        return ""
    text = text.translate(_INVISIBLE)
    text = "".join(_EXPAND.get(ch, ch) for ch in text)
    text = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(text.split())


def squeeze(text: str) -> str:
    """`N()` with the separators between *single characters* removed.

    Stacked vertical text is drawn one glyph per operator, so extractors return it one
    glyph per line. Compared with its separators intact, a whole run matches a single
    character; joined up, it matches itself (docs/metrics/core.md).

    Only runs of single characters are joined, and that restriction is not fussiness.
    Deleting every space instead manufactures adjacencies the page never had: the phone
    number `+44 7669 074391` becomes `+447669074391`, which contains `6907` - the
    identifying tail of an unrelated account number elsewhere on the same sheet. That
    reports a correctly redacted probe as a leak, and no amount of care in the generator
    can prevent it, because the collision only exists after the squeeze.
    """
    out: list[str] = []
    run: list[str] = []
    for token in normalize(text).split(" "):
        if len(token) == 1:
            run.append(token)
            continue
        if run:
            out.append("".join(run))
            run = []
        out.append(token)
    if run:
        out.append("".join(run))
    return " ".join(out)


def lcs_length(a: str, b: str) -> int:
    """Length of the longest common *substring* - the quantity the leak test uses.

    Binary search on the length, with the window test delegated to `in`. A common
    substring of length L implies one of length L-1, so the predicate is monotone and the
    search is exact. The textbook DP is quadratic in `len(b)`, and `b` here is a whole
    page of extracted text tested against hundreds of units.
    """
    if not a or not b:
        return 0
    low, high = 0, min(len(a), len(b))
    while low < high:
        mid = (low + high + 1) // 2
        if any(a[i : i + mid] in b for i in range(len(a) - mid + 1)):
            low = mid
        else:
            high = mid - 1
    return low


def lcs(a: str, b: str) -> str:
    """The longest common substring itself, for the evidence field."""
    length = lcs_length(a, b)
    if not length:
        return ""
    for i in range(len(a) - length + 1):
        if a[i : i + length] in b:
            return a[i : i + length]
    return ""  # pragma: no cover - unreachable while lcs_length is exact


@dataclass(frozen=True, slots=True)
class Disclosure:
    """What survived, and which test caught it."""

    leaked: bool
    rdr: float
    matched: str
    unit: str
    test: str  # "proportional" | "absolute" | "none"

    @property
    def grade(self) -> str:
        """docs/metrics/core.md: "leaked" alone is not actionable."""
        if not self.leaked:
            return "redacted"
        return "full" if self.rdr >= 1.0 else "partial"


NOT_DISCLOSED = Disclosure(False, 0.0, "", "", "none")


def disclosure(
    units: tuple[str, ...] | list[str],
    haystack: str,
    *,
    tau_text: float = 0.5,
    l_min: int = 4,
    d_c: int | None = None,
) -> Disclosure:
    """Residual disclosure of a probe's units in `haystack`.

    Three escapes, three tests, and a tool has to defeat all of them: a value left mostly
    intact (proportional), an identifying tail left behind (absolute), and an identifying
    token left behind (the units, which the generator supplies because it built the value
    and knows which fragments identify).

    `d_c` is the category's absolute disclosure length; `None` disables that test, which
    is right for names and free text where no short prefix means anything.

    The absolute test fires on a unit that survives **whole**, not on any run of `d_c`
    characters. That is what the worked examples in docs/metrics/core.md describe - the
    national id masked to `XXX-XX-6789` leaves the unit `6789` intact, and the email
    reduced to `@acme.com` leaves the unit `acme` intact - and it is the only reading
    that stays a disclosure test. A floating four-character run is a coincidence, not a
    leak: the email domain `postcrate` shares `rate` with ordinary prose, and scoring
    that as a disclosed address would report a correct redaction as a failure. The
    generator's collision guard rejects a unit that appears in full anywhere else on the
    page, so the two halves agree on exactly this definition.
    """
    if not haystack:
        return NOT_DISCLOSED

    plain, squeezed = normalize(haystack), squeeze(haystack)
    best = NOT_DISCLOSED
    for unit in units:
        n_unit = normalize(unit)
        if not n_unit:
            continue
        # The squeezed comparison must be scored against the squeezed unit, or a value
        # with spaces would be handed a ratio above 1.
        matched = lcs(n_unit, plain)
        # The unit loses every space; the haystack only loses the ones between single
        # characters. A stacked run then matches the whole value, while ordinary prose
        # keeps the word boundaries that stop a short unit matching across them.
        s_unit = n_unit.replace(" ", "")
        s_matched = lcs(s_unit, squeezed)
        if len(s_matched) > len(matched):
            matched, n_unit = s_matched, s_unit

        rdr = len(matched) / len(n_unit)
        if rdr >= tau_text and len(matched) >= l_min:
            test = "proportional"
        elif d_c is not None and len(matched) >= d_c and matched == n_unit:
            test = "absolute"
        else:
            test = "none"

        leaked = test != "none"
        # Any surviving unit is a leak; among leaks, report the worst.
        if (leaked, rdr) > (best.leaked, best.rdr):
            best = Disclosure(leaked, rdr, matched, unit, test)
    return best
