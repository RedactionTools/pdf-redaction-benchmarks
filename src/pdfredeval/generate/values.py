"""Seeded synthetic values, with their disclosure units and severity.

Every value is invented. Nothing here is drawn from a real person or document, which is
what makes it safe to upload a case to a third-party service.

Two details that look cosmetic and are not:

* **Checksums are valid.** Card numbers pass Luhn and IBANs pass mod-97, because PII
  detectors commonly validate them. A malformed test value would be skipped by a good
  detector and score as a miss, understating its recall.
* **Disclosure units are at least `L_min` characters.** A shorter unit can never trigger
  the proportional leak test, so emitting one would create a probe that cannot be scored.
"""

from __future__ import annotations

import random
import string
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from ..textmatch import lcs_length
from ..types import Severity

L_MIN = 4  # docs/metrics/core.md
TAU_TEXT = 0.5  # docs/metrics/core.md


def _grams(text: str, size: int = L_MIN) -> set[str]:
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def _variants(folded: str) -> tuple[str, ...]:
    """A string as the scorer may meet it: as written, and with its spaces gone."""
    tight = folded.replace(" ", "")
    return (folded,) if tight == folded else (folded, tight)

# Pools chosen so no entry is a substring of another: the scorer hunts each unit across
# the whole output, so an overlap would make a correct redaction look like a leak.
FIRST_NAMES = (
    "Whitfield", "Marianne", "Gustav", "Priya", "Kenji", "Oluwaseun", "Bettina", "Rafael",
    "Ingrid", "Tobias", "Yelena", "Casper", "Noemi", "Radek", "Sunniva", "Hamish", "Lucia",
    "Freya", "Dmytro", "Beatrix", "Jonas", "Aurelie", "Mikkel", "Bartosz",
    "Cosima", "Dagfinn", "Eilidh", "Fionnuala", "Gunther", "Hyacinth", "Isolde", "Jarrah",
    "Kwabena", "Leocadia", "Matthias", "Nkechi", "Ottilie", "Pavlina", "Quentin",
    "Rosalind", "Severin", "Thandiwe", "Ulrike", "Wilhelmina", "Xiomara", "Yusuf",
    "Zephyrine", "Adaeze", "Bronwen", "Csilla", "Dermot", "Gwendolyn", "Halvard", "Ilkay",
    "Joaquin", "Katarzyna", "Lorcan", "Mirembe", "Oskar", "Perpetua", "Rohan", "Solveig",
    "Torquil", "Vasilis", "Wanjiru", "Zsofia", "Cleo", "Duarte", "Elif", "Farhan", "Giulia",
)
SURNAMES = (
    "Diffie", "Okonkwo", "Bhattacharya", "Moravec", "Nakamura", "Oyelaran", "Vandenberg",
    "Krishnan", "Szabolcs", "Waclawek", "Ferreira", "Grimaldi", "Hollowell", "Iversen",
    "Janowicz", "Kowalczyk", "Lundgren", "Nowosielski", "Ostrowska", "Abernathy",
    "Blackwood", "Castellano", "Dziedzic", "Fontaine", "Gruenwald", "Ivanovic", "Jorgensen",
    "Kaminski", "Lefevre", "Magnusson", "Nieminen", "Olufemi", "Prokopenko", "Rasmussen",
    "Ueda", "Valdes", "Yamamoto", "Achebe", "Beaumont", "Chukwu", "Delacroix",
    "Espinoza", "Gallagher", "Hernandez", "Ignatiev", "Kirchner", "Lombardi", "Mbeki",
    "Novotny", "Ogundipe", "Pettersson", "Rahimi", "Sandoval", "Ulloa", "Verhoeven",
    "Yildirim",
)
STREETS = (
    "Marketgate", "Wexbury", "Pinecrest", "Rosslyn", "Dunmore", "Ellsworth", "Ashgrove",
    "Cobblestone", "Dovecote", "Elmridge", "Foxglove", "Greystoke", "Inglewood", "Juniper",
    "Kingsmead", "Larkspur", "Meadowvale", "Northgate", "Oakhurst", "Primrose", "Underhill",
    "Vineyard", "Westcliff", "Yarrowmere",
)
CITIES = (
    "Carnforth", "Hollyvale", "Inverleith", "Jedborough", "Kirkwall", "Langholm",
    "Nantwich", "Penrith", "Saltburn", "Tarbert", "Yeovil", "Ashbourne", "Brackley",
    "Cheadle",
)
EMPLOYERS = (
    "Kestrelware", "Quarrystone", "Silverkeep", "Thistledown", "Amberforge", "Copperline",
    "Frostmere", "Jadeworks", "Lanternhouse", "Nightjar",
)
MAIL_DOMAINS = (
    "inboxery", "postcrate", "mailhaven", "sendcove", "parcelnote", "dispatchly",
    "courierly",
)
CONDITIONS_MED = (
    "Sarcoidosis", "Retinopathy", "Endometriosis", "Gastroparesis", "Myasthenia",
    "Osteopenia", "Pancreatitis", "Sialadenitis", "Thalassemia", "Vasculitis",
)
JOB_TITLES = (
    "Radiographer", "Surveyor", "Actuary", "Toolmaker", "Ergonomist", "Hydrologist",
    "Upholsterer",
)
DISTRACTOR_LABELS = (
    ("invoice_no", "Invoice no"), ("sku", "SKU"), ("order_id", "Order"),
    ("vat_id", "VAT reg"), ("part_no", "Part"), ("total", "Total"),
)


@dataclass(frozen=True, slots=True)
class Value:
    """A generated value, ready to become a probe."""

    category: str
    text: str
    units: tuple[str, ...]
    severity: Severity
    label: str | None = None
    must_redact: bool = True
    ambiguous: bool = False
    rationale: str | None = None


#: category -> severity. What counts as a *fragment* of the value that identifies on its
#: own is expressed as a disclosure unit by each maker below, not as a length here: "any
#: run of N characters" would match a TLD or a coincidental pair of digits.
SPEC: dict[str, Severity] = {
    "PERSON": Severity.HIGH,
    "EMAIL": Severity.HIGH,
    "PHONE": Severity.HIGH,
    "ADDRESS": Severity.HIGH,
    "NATIONAL_ID": Severity.CRITICAL,
    "CARD": Severity.CRITICAL,
    "IBAN": Severity.CRITICAL,
    "PASSPORT": Severity.CRITICAL,
    "PLATE": Severity.MEDIUM,
    "POSTCODE": Severity.MEDIUM,
    "DOB": Severity.MEDIUM,
    "EMPLOYER": Severity.MEDIUM,
    "JOB_TITLE": Severity.MEDIUM,
    "CONDITION": Severity.CRITICAL,
    "ACCOUNT": Severity.HIGH,
    "IP": Severity.LOW,
    "CITY": Severity.LOW,
}


#: Every label the factory can attach to a value. Reserved at construction, because
#: a label is drawn on the page and the scorer hunts each unit across the whole
#: output: a surname sharing four characters with "National ID" would score a
#: correct redaction as a leak. The generator is the only place that can prevent it.
LABELS: tuple[str, ...] = (
    "Account",
    "Address",
    "Card",
    "Date of birth",
    "Diagnosis",
    "Email",
    "IBAN",
    "Name",
    "National ID",
    "Passport",
    "Phone",
    "Plate",
    "Postcode",
    "Supplier",
)


def luhn_check_digit(digits: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def iban_check_digits(country: str, bban: str) -> str:
    """mod-97-10 per ISO 13616, so checksum-validating detectors accept the value."""
    rearranged = bban + country + "00"
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    return f"{98 - int(numeric) % 97:02d}"


class ValueFactory:
    """Seeded factory that refuses to emit colliding values.

    Collision here means *substring* overlap, not equality: the scorer's leak test uses
    the longest common substring, so a unit contained in any other string on the page
    would make a correct redaction score as a leak.
    """

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self._corpus: list[str] = []
        self._gram_index: dict[str, list[int]] = {}
        self._used: dict[int, set[str]] = {}
        for label in (*LABELS, *(text for _, text in DISTRACTOR_LABELS)):
            self.reserve_text(label)

    # --- collision control --------------------------------------------------------

    def reserve_text(self, text: str) -> None:
        """Register non-probe text (labels, banner, boilerplate) so probes avoid it."""
        self._admit(text.casefold())

    def _admit(self, folded: str) -> None:
        for variant in _variants(folded):
            index = len(self._corpus)
            self._corpus.append(variant)
            for gram in _grams(variant):
                self._gram_index.setdefault(gram, []).append(index)

    def conflicts(self, units: Sequence[str]) -> str | None:
        """Would any unit trip the scorer's leak test against text already on the page?

        This mirrors `docs/metrics/core.md` rather than merely checking for duplicates.
        The scorer compares by longest common substring, so two *different* strings that
        share a long enough run collide: redact `Whitfield` perfectly and the probe still
        scores as leaked if `Thornfield` survives elsewhere on the page. The generator is
        the only place that can prevent it.

        Both spacings of every string are tested, because the scorer tests both. An
        extractor that returns `+44 7669 074391` without its spaces hands the leak test
        the run `6907`, which is the identifying tail of an account number that shares
        no visible substring with the phone number at all.
        """
        for unit in units:
            for folded in _variants(unit.casefold()):
                if len(folded) < L_MIN:
                    continue
                for index in self._candidates(folded):
                    other = self._corpus[index]
                    run = lcs_length(folded, other)
                    if run >= L_MIN and (run / len(folded) >= TAU_TEXT
                                         or run / len(other) >= TAU_TEXT):
                        return (
                            f"{unit!r} shares {run} characters with existing text "
                            f"{other!r}, enough for the leak test to match"
                        )
        return None

    def _candidates(self, folded: str) -> set[int]:
        """Corpus entries worth a full comparison.

        A common run of `L_MIN` characters implies a shared `L_MIN`-gram, so the gram
        index rules out almost every pair without running the quadratic comparison.
        """
        found: set[int] = set()
        for gram in _grams(folded):
            found.update(self._gram_index.get(gram, ()))
        return found

    def _commit(self, value: Value) -> Value:
        """Accept a candidate, or raise. Units shorter than `L_MIN` are dropped.

        A unit below `L_MIN` can never satisfy the proportional leak test, so a probe
        whose every unit is that short could not be scored at all.
        """
        units = tuple(u for u in value.units if len(u) >= L_MIN)
        if not units:
            raise ValueError(
                f"{value.category} value {value.text!r} has no unit of at least {L_MIN} "
                f"characters, so no leak test could ever fire on it"
            )
        value = replace(value, units=units)
        problem = self.conflicts(units)
        if problem:
            raise Collision(problem)
        for unit in units:
            self._admit(unit.casefold())
        self._admit(value.text.casefold())
        return value

    def unique(self, make: Callable[[], Value], *, tries: int = 40) -> Value:
        """Draw until the value clears the collision check."""
        last = ""
        for _ in range(tries):
            try:
                return self._commit(make())
            except PoolExhausted:
                raise  # retrying cannot help
            except Collision as exc:
                last = str(exc)
        raise Collision(f"no collision-free value after {tries} tries ({last})")

    def _pick(self, pool: Sequence[str]) -> str:
        """Draw without replacement.

        Pools are mutually non-overlapping, so an unused entry can never collide with a
        committed one. Sampling without replacement means a page of 20 names costs 20
        draws instead of a retry storm, and running dry is a precise error rather than
        an exhausted retry budget.
        """
        used = self._used.setdefault(id(pool), set())
        free = [entry for entry in pool if entry not in used]
        if not free:
            raise PoolExhausted(
                f"pool of {len(pool)} entries is exhausted; add entries to generate more "
                f"probes of this kind on one page"
            )
        choice = self.rng.choice(free)
        used.add(choice)
        return choice

    def _digits(self, n: int) -> str:
        return "".join(self.rng.choice(string.digits) for _ in range(n))

    # --- targets -------------------------------------------------------------------

    def person(self) -> Value:
        first, last = self._pick(FIRST_NAMES), self._pick(SURNAMES)
        full = f"{first} {last}"
        return Value("PERSON", full, (full, first, last), SPEC["PERSON"], label="Name")

    def short_person(self) -> Value:
        """A surname on its own, for runs whose height is bounded by their length.

        A stacked column of a full name can be 22 glyphs tall, which would claim a third
        of the page for one probe. Real vertical text - a spine, a table header - is
        short, so the shorter value is the more faithful one here as well as the one
        that fits.
        """
        try:
            name = self._pick(SURNAMES)
        except PoolExhausted:
            # A given name on its own is just as good a probe, and doubles how many
            # single-token values one page can carry.
            name = self._pick(FIRST_NAMES)
        return Value("PERSON", name, (name,), SPEC["PERSON"], label="Name")

    def matrix_person(self, max_glyphs: int, *, tries: int = 40) -> Value:
        """The one subject an entire condition matrix shares: a full name, short.

        Drawn once and reused for every cell, so a cell's outcome says something about
        its rendering and never about which value it drew. A full first-and-last name on
        purpose: the matrix asks whether the tool reads *the subject* everywhere, and a
        survivor in any cell means the subject is still disclosed. Short because the
        tallest stacked run on the page is as tall as this name is long.
        """
        for _ in range(tries):
            first, last = self._pick(FIRST_NAMES), self._pick(SURNAMES)
            if len(first) + len(last) + 1 <= max_glyphs:
                full = f"{first} {last}"
                return Value("PERSON", full, (full, first, last),
                             SPEC["PERSON"], label="Name")
        raise PoolExhausted(
            f"no first-and-last pair within {max_glyphs} glyphs after {tries} draws; "
            f"widen the pools or the budget"
        )

    def email(self) -> Value:
        first, last = self._pick(FIRST_NAMES), self._pick(SURNAMES)
        domain = f"{self._pick(MAIL_DOMAINS)}.example"
        label_only = domain.split(".")[0]
        text = f"{first.lower()}.{last.lower()}@{domain}"
        # The domain *label* identifies the employer; the TLD identifies nobody, and
        # making the whole domain a unit would collide every address on the page.
        return Value("EMAIL", text,
                     (text, first.lower(), last.lower(), label_only),
                     SPEC["EMAIL"], label="Email")

    def phone(self) -> Value:
        text = f"+44 7{self._digits(3)} {self._digits(6)}"
        return Value("PHONE", text, (text, text[-6:]), SPEC["PHONE"], label="Phone")

    def national_id(self) -> Value:
        text = f"{self._digits(3)}-{self._digits(2)}-{self._digits(4)}"
        # The last four alone re-identify, which is the whole point of the probe.
        return Value("NATIONAL_ID", text, (text, text[-4:]), SPEC["NATIONAL_ID"],
                     label="National ID")

    def card(self) -> Value:
        body = "4111" + self._digits(11)
        text = body + luhn_check_digit(body)
        return Value("CARD", text, (text, text[-4:]), SPEC["CARD"], label="Card")

    def iban(self) -> Value:
        bban = "NWBK" + self._digits(14)
        text = "GB" + iban_check_digits("GB", bban) + bban
        return Value("IBAN", text, (text, text[-6:]), SPEC["IBAN"], label="IBAN")

    def passport(self) -> Value:
        text = self.rng.choice("PXCK") + self._digits(8)
        return Value("PASSPORT", text, (text, text[-4:]), SPEC["PASSPORT"],
                     label="Passport")

    def plate(self) -> Value:
        letters = "".join(self.rng.choice(string.ascii_uppercase) for _ in range(3))
        text = f"{letters}{self._digits(3)}"
        return Value("PLATE", text, (text,), SPEC["PLATE"], label="Plate")

    def postcode(self) -> Value:
        text = f"{self.rng.choice('ABCDEFGH')}{self._digits(2)} {self._digits(1)}XQ"
        return Value("POSTCODE", text, (text,), SPEC["POSTCODE"], label="Postcode")

    def dob(self) -> Value:
        day, month = self.rng.randint(1, 28), self.rng.randint(1, 12)
        text = f"{day:02d}/{month:02d}/19{self.rng.randint(50, 99)}"
        return Value("DOB", text, (text,), SPEC["DOB"], label="Date of birth")

    def address(self) -> Value:
        street = self._pick(STREETS)
        city = self._pick(CITIES)
        text = f"{self.rng.randint(1, 180)} {street} Road, {city}"
        # `Road` identifies nobody and is not a unit.
        return Value("ADDRESS", text, (text, street, city), SPEC["ADDRESS"],
                     label="Address")

    def condition(self) -> Value:
        text = self._pick(CONDITIONS_MED)
        return Value("CONDITION", text, (text,), SPEC["CONDITION"], label="Diagnosis")

    def account(self) -> Value:
        text = f"{self._digits(8)}"
        return Value("ACCOUNT", text, (text, text[-4:]), SPEC["ACCOUNT"],
                     label="Account")

    # --- distractors: must survive -------------------------------------------------

    def distractor(self) -> Value:
        kind, label = self.rng.choice(DISTRACTOR_LABELS)
        text = {
            "invoice_no": lambda: f"INV-{self._digits(6)}",
            "sku": lambda: f"SKU-{self.rng.choice('QRSTU')}{self._digits(5)}",
            "order_id": lambda: f"ORD{self._digits(7)}",
            "vat_id": lambda: f"GB{self._digits(9)}",
            "part_no": lambda: f"P{self._digits(4)}-{self._digits(3)}",
            "total": lambda: f"{self.rng.randint(100, 9999)}.{self._digits(2)} GBP",
        }[kind]()
        return Value(kind.upper(), text, (text,), Severity.LOW, label=label,
                     must_redact=False)

    def employer_distractor(self) -> Value:
        """A public company name: sensitive in a patient record, public on an invoice."""
        text = f"{self._pick(EMPLOYERS)} Holdings"
        return Value("EMPLOYER", text, (text,), SPEC["EMPLOYER"], label="Supplier",
                     must_redact=False, ambiguous=True,
                     rationale="public on an invoice, identifying in a personnel record")

    # --- convenience ---------------------------------------------------------------

    TARGET_MAKERS: tuple[str, ...] = (
        "person", "email", "phone", "national_id", "card", "iban", "passport",
        "plate", "postcode", "dob", "address", "condition", "account",
    )

    def target(self, kind: str | None = None) -> Value:
        maker = getattr(self, kind or self.rng.choice(self.TARGET_MAKERS))
        return self.unique(maker)


class Collision(ValueError):
    """A candidate value overlapped text already on the page."""


class PoolExhausted(Collision):
    """A name pool ran out, so no further distinct value of that kind is available.

    Distinct from a collision: retrying cannot help, the pool needs more entries.
    """
