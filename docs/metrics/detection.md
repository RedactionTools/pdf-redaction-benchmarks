# Detection metrics

Given content the tool has extracted, did it recognise what is sensitive? This is the stage
where tools differ most, and where the ground truth is genuinely arguable.

Notation from [core.md](core.md).

## Two observation modes

Detection is only *directly* measurable when the tool tells us what it found.

| Mode | When | What can be measured |
|---|---|---|
| **Observed** | the adapter returns an entity list — common on the API path | detection precision, recall, type accuracy, confusion |
| **Inferred** | UI-only tools: the redacted PDF is the sole output | nothing separable. A miss here is indistinguishable from a redaction failure |

In inferred mode, report redaction outcomes and say plainly that detection is
unidentifiable. Do not publish a "detection recall" that is really a leak rate wearing a
different label.

## Text PII

| Group | Examples |
|---|---|
| Direct identifiers | person name, email, phone, national id, passport, IBAN / card, plate, username |
| Quasi-identifiers | address, date of birth, employer, job title, age, gender |
| Contextual | medical condition, salary, case or account number, IP, free text naming a person |

**PII is contextual, so the dataset must be too.** "Acme Ltd, 5 Market St" is public on an
invoice and sensitive in a patient record; a bare nine-digit number may be an SSN or an
invoice number. Probes come in three flavours:

| Flavour | `m_i` | Measures |
|---|---|---|
| Target | 1 | recall — the privacy axis |
| Distractor | 0 | over-redaction — the utility axis |
| Ambiguous | marked, with a written rationale | judgement; reported separately |

Ambiguous probes are scored apart and never folded into the headline. Burying a defensible
disagreement inside one number makes the whole benchmark look arbitrary to a vendor. What
*is* published for them is the **agreement rate**: the fraction where the tool's call
matches ours, with the disagreements listed.

## Objects

| Class | Notes |
|---|---|
| Face | ID-photo crops, group photos, drawn faces, partial and profile |
| Signature | handwritten, stamped, digitised image, ink on a ruled line |
| QR / barcode | the payload is ground truth — see the decode test below |
| Stamp, seal | overlapping text, rotated, translucent |
| Logo | identifies the organisation; often out of a tool's scope, so scored separately |

Codes get a second test that needs no thresholds at all: **run a decoder over the output**.
If the payload still decodes, it leaked — regardless of coverage, regardless of how redacted
it looks. Binary and unarguable, so it overrides `Cov` for this class.

## Observed-mode metrics

Match each reported entity `p` to a ground-truth probe `i`:

```
text:   jaccard( span_p, span_i ) ≥ τ_span      (char offsets, default 0.5)
region: IoU( box_p, B_i )         ≥ τ_iou       (default 0.5)
```

Assign greedily, highest overlap first, one-to-one.

> IoU is right *here* and wrong for redaction coverage (→ [core.md](core.md)). Here we judge
> a box the tool chose to report, so overshoot is a real error. There we ask whether a target
> is covered, where overshoot is a utility cost measured elsewhere.

| Metric | Measures | Formula |
|---|---|---|
| **Detection Recall** | targets found | `matched targets / all targets` |
| **Detection Precision** | reported entities that were real | `matched entities / all reported entities` |
| **F1** | their harmonic mean | standard |
| **Type Accuracy** | correctly *labelled*, among those found | `correctly typed / matched` |
| **Confusion** | which categories are mistaken for which | matrix over `c_i × c_p` |

Precision is legitimate here — its denominator is the tool's own output, not a target /
distractor mix we chose. That is exactly why it is *not* used for probe outcomes
(→ [core.md](core.md)).

## Detected vs. correctly typed

Two distinct failures, usually collapsed into one number:

| Situation | Redaction outcome | Detection outcome |
|---|---|---|
| name found, labelled `PERSON` | removed | correct |
| name found, labelled `ORG` | removed — privacy intact | type error |
| name not found | leak | miss |

Only the third is a privacy failure. Type errors still matter for tools with per-type
controls ("redact names, keep companies"), where a mislabel becomes a leak the moment the
user narrows the settings — so they are reported, but not as leaks.

## Difficulty tiers

Every probe is tiered, so scores stay comparable as the dataset grows:

| Tier | Example |
|---|---|
| easy | labelled field, clean font — `Email: a@b.com` |
| medium | unlabelled, mid-sentence, abbreviated |
| hard | split across lines, inside a table cell, inside an image, non-Latin script |

Report `LR` per tier. A tool at 0.9 easy / 0.3 hard is a different product from a uniform
0.6, and the pooled number hides which one you are buying.
