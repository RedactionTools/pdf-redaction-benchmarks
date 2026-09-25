# Datasets

Two families, with different jobs.

| | End-to-end | Per-stage probes |
|---|---|---|
| Looks like | a real invoice, statement, email, medical report | a dense, deliberately artificial page |
| Answers | "how good is this tool on my documents?" | "*why* did it fail?" |
| Probes per page | few, naturally placed | many, packed |
| Risk | an unrepresentative sample of document types | tools may behave differently on unnatural layouts |

Both are needed: the first is what users care about, the second is what vendors can act on.
The two are reported side by side, never merged.

## End-to-end: segmentation

| Axis | Values |
|---|---|
| Language | en, uk, de, fr, … — scripts and diacritics stress OCR and name detection |
| Document type | invoice, bank statement, email, medical report, contract, ID scan, … |
| Origin | born-digital text layer · scan-only raster · scan + OCR layer |

`Origin` is the axis most often forgotten and the most predictive of failure.

## Per-stage probe families

| Family | One page of | Segmented by |
|---|---|---|
| `pii-detection` | text PII, objects, distractors | category, difficulty tier |
| `extraction-conditions` | the same value under varied rendering | orientation, polarity, provenance, scale |

The condition matrix gets its own page because its 22 probes would not fit beside the PII
page's 30–60 (→ [../metrics/extraction.md](../metrics/extraction.md)).

## Per-stage probes: packing the page

A probe is one independently scored item with known ground truth. Because we get **one
page**, the page is packed — but packed as a *document*, not as a list. Probes sit in
titled sections of aligned, equal-width columns, and each section's column count follows
the widest field it holds: an email needs half the page, a licence plate a quarter. A
`pii-detection` sheet carries around **85 probes**, turning n=1 into n≈85.

Structure is not decoration. A page a person cannot read is a page an operator cannot
check, and a ragged one is harder to compare probe against probe. Packing stays the
default on the API path too — it costs nothing there, and one dataset then serves both.

Targets and distractors are **mixed within every section**, never pooled into one of their
own. Grouping them would make position a tell, and no real document separates its
sensitive fields from its ordinary ones.

```
┌─[F]──────────────────────────────────────────[F]─┐
│  header zone — strings that also live in metadata│
├──────────────────────────────┬───────────────────┤
│ TEXT PROBES                  │ OBJECT PROBES     │
│  #01 name      #05 email     │  [ face ]  [ QR ] │
│  #02 iban      #06 phone     │  [ sig  ]  [stamp]│
│  #03 ssn       #07 date      │  [ logo ]  [ bar ]│
│  #04 addr      #08 id-no     │                   │
├──────────────────────────────┴───────────────────┤
│ DISTRACTORS — look sensitive, must survive       │
│  invoice no · SKU · public company address       │
├──────────────────────────────────────────────────┤
│ TRAPS — invisible · in-annotation · CID · outline│
│ CONDITIONS — vertical · inverse · skew · cursive │
└─[F]──────────────────────────────────────────[F]─┘
   [F] = fiducial mark, printed by our generator
```

Design rules:

- **Isolated, twice over.** Spatially — probes never touch, so one redaction box cannot
  ambiguously cover two. And *lexically* — no identifying token is reused by a different
  value anywhere else on the page. A distractor vendor named "Smith & Co" beside a "John
  Smith" probe makes a correct redaction score as a leak, because the scorer hunts the
  token `Smith` across the whole output. The generator owns both constraints; nothing
  downstream can repair them.

  Uniqueness is per value, not per probe: the `extraction-conditions` matrix plants one
  subject under many renderings, so every cell carries the same name's units on purpose.
  A surviving copy then reads as a leak on all of that subject's cells — the honest
  per-subject reading, since one survivor is enough to identify the person — while a
  token shared between *different* values is always a defect.

  "Anywhere else" includes the page's own furniture. A field label, a section title and a
  line of boilerplate are all drawn on the sheet, so a surname sharing a long enough run
  with `National ID` is the same failure as one colliding with a distractor. Every label
  and heading is registered with the value factory **before** a value is drawn — the
  factory can redraw a value it has not handed out yet, and cannot un-choose one it has —
  and the builder then refuses to emit a case where any unit still trips the scorer's own
  leak test against the rest of the page.
- **Addressable.** Each carries a visible index, so a human reviewing the output can name
  what leaked.
- **Balanced.** Every page carries distractors; a page of pure PII rewards
  redact-everything.
- **Registered.** Fiducials in all four corners let the aligner recover the mapping back to
  ground-truth coordinates even after rasterisation, rotation or resize.

## The provenance banner

Every generated page carries a banner across its head: the Redaction Tools mark, the tool
and version that produced it, the case id, the generation timestamp, and links to
[redaction-tools.com](https://redaction-tools.com) and the
[generator's repository](https://github.com/RedactionTools/pdf-redaction-benchmarks).

A case travels — uploaded to a stranger's web form, downloaded, printed, screenshotted,
pasted into a ticket — and by the time a result is disputed the page may be all that is
left of the evidence. The banner is reserved out of the layout, so a probe can never be
laid over it, and its strings are reserved in the value factory, so no probe value can
collide with them.

**The seed is not on the page, and not in the metadata.** The page is handed to the vendor
being measured, and the seed regenerates the case — including a holdout case. It lives in
`ground_truth.json`, which never leaves us.

> A case id derived from the seed (the default, `family-42`) is itself a hint. For the
> holdout split, pass an opaque `--case-id`.

## Ground truth

Emitted beside each PDF. Per probe, conceptually:

| Field | Meaning |
|---|---|
| id, kind | `text_span` · `image` · `face` · `signature` · `code` · `stamp` · `metadata_key` |
| category | PII type for text; object class otherwise |
| location | page and box in PDF points; character span for text |
| value | the exact string / image hash / decoded payload to hunt for in the output |
| units | the value's **disclosure units** — itself, plus each token that identifies on its own. `Whitfield Diffie` → `{Whitfield, Diffie}`; `5 Market Street` → `{Market}`, since `Street` identifies nobody. Scoring tests each → [metrics/core.md](../metrics/core.md) |
| D_c | shortest run of the value's own characters that still discloses, for structured types (card, id, phone): 4 |
| must_redact | `true` for targets, `false` for distractors |
| severity | how bad a leak of this probe is → [metrics/core.md](../metrics/core.md) |
| difficulty | tier, so scores stay comparable as the dataset grows |
| trap | which *structural* extraction trap this probe exercises, if any |
| conditions | the *rendering* segment: orientation · polarity · provenance · scale — e.g. `vertical / inverse / hand-cursive / pt6`. One level per axis → [metrics/extraction.md](../metrics/extraction.md) |
| ambiguous, rationale | the call is genuinely arguable — an employer name is public on an invoice and identifying in a personnel record. Held out of every rate and published as an agreement rate → [metrics/detection.md](../metrics/detection.md) |
| page_size, fiducials | page geometry and the four registration marks, so the aligner can map an output back → [metrics/core.md](../metrics/core.md) |

`value` is what makes automated scoring possible: after alignment, a leak check reduces to
"does this value still appear anywhere in the output?"

## Generation

Procedural and **seeded** — a seed plus a generation timestamp reproduces the
byte-identical PDF. The timestamp is part of the input because every page carries one in
its banner; the ground truth records the value used, so any case can be regenerated
exactly (`--generated-at`, or `SOURCE_DATE_EPOCH`).
Implemented in `src/pdfredeval/generate/`: `values.py` (seeded values and
their disclosure units), `builder.py` (drawing and ground truth), `families.py` (the case
families), `pdf.py` (a minimal writer, because the structures this benchmark needs are the
ones a high-level library hides), `raster.py` and `textraster.py` (pixels, for probes whose
value must not have a text layer).

Fonts are **bundled**, not discovered on the host: a system font would make the same case
id produce different bytes on macOS and on Linux CI, and byte-identical regeneration is the
property everything else rests on. Rasterised conditions need Pillow
(`uv sync --extra generate`); without it those cells are reported as skipped rather than
quietly dropped.

The builder refuses to emit a case that breaks an invariant the scorer depends on: probe
boxes that overlap, a disclosure unit shared by two probes, a value absent from the PDF
bytes, a leaked graphics state, or a broken xref. A silently malformed case corrupts every
score computed from it, so these are errors rather than warnings.

| Split | Seeds | Purpose |
|---|---|---|
| `public` | published with the generator | development, reproduction, vendor self-testing |
| `holdout` | private | leaderboard; resists training on our own data |

Our datasets are public and redaction tools train on public data, so a fixed public corpus
decays into a training set. Seeded generation means the holdout can be regenerated fresh
whenever it looks compromised.

## Distribution

Hugging Face Hub, one dataset repo per family:

```
pdfredeval/<family>
  cases/<case_id>/{<case_id>.pdf, ground_truth.json}
  index.parquet        # one row per probe, for pandas
  README.md            # card: axes, splits, generator version
```

Scores always cite a **revision**, never `main`.
