# pdf-redaction:web, extraction-conditions, v0.1.1

54 probes on 1 page · 1 run
**42 passed · 12 failed**
Dataset revision `v0.1.1`.

## Summary

- pdf-redaction:web leaked 12 of 54 (22%) sensitive values.
- 93% of the page's text can still be selected and searched.
- Leaks were found in: rendered_pixels (visible to anyone who opens the file); ocr (catches translucent or hairline covers); content_stream (copy-paste, pdftotext, any parser).

## How to read this report

- **Probe** - one value planted on the test page with a known right answer. Some are sensitive and must be removed (a card number, a name); some are harmless look-alikes that must stay (an invoice number).
- **Leak rate** - the share of sensitive values that can still be recovered from the output - by looking, copy-paste, a parser or the file's hidden parts. 0 is best. This is the main result.
- **Over-redaction** - the share of harmless values the tool removed anyway. 0 is best. A tool that blacks out everything scores 0 leak and 1 here, which is why both are always shown.
- **[low, high] n=** - `0.66 [0.58, 0.73] n=162` means 0.66 measured on 162 probes, and the true rate is between 0.58 and 0.73 with 95% confidence. Small n gives a wide range; compare tools by whether their ranges overlap.
- **Layers** - the places inside a PDF a value can survive: what is drawn on the page, the text you can copy, metadata, attachments, older saved versions, and more. A black box drawn over text that is still copyable is a leak.
- **Checks** - whether the output is still a usable document. A tool that turns the page into a picture removes everything, so it looks perfect on leak rate and fails here.

## Results

### Headline

| Metric | Value | Better | What it means |
|:---|---:|:---|:---|
| Leak rate (LR) | 0.222 [0.13, 0.35] n=54 | 0 is best | share of sensitive values still recoverable from the output |
| Over-redaction rate (ORR) | n/a | 0 is best | share of values that should have stayed but were removed |
| Weighted leak rate (wLR) | 0.222 | 0 is best | leak rate with severe categories counting more |
| Text retention (TR) | 0.932 | 1 is best | share of the page's text still selectable - is the document usable |
| Area over-coverage (AOC) | 0.0000 | 0 is best | share of the page blacked out that did not need to be |
| RQS (beta=2) | n/a | 1 is best | one-number score for sorting only; LR and ORR are the result |

_A rate reads `value [low, high] n=count`: the value, the range the true rate lies in with 95% confidence, and how many probes it rests on._

### Outcomes

| Outcome | Probes | Meaning |
|:---|---:|:---|
| Removed, as it should be (TP) | 42 | sensitive value gone - good |
| Leaked (FN) | 12 | sensitive value still recoverable - the privacy failure |
| Removed by mistake (FP) | 0 | harmless value destroyed - a utility loss |
| Kept, as it should be (TN) | 0 | harmless value left intact - good |
| Out of scope (unsupported) | 0 | the tool says it does not handle this; not counted as a miss |
| Could not check (undecided) | 0 | no layer could be read; left out of every rate |
| Arguable (ambiguous) | 0 | reasonable people could disagree; scored apart |

_Every probe lands in exactly one row: 54 of 54. Leak rate = Leaked / (Leaked + Removed); over-redaction = Removed by mistake / (Removed by mistake + Kept)._

### Is the document still usable?

| Check | Result | What it means |
|:---|:---|:---|
| all gates | pass | all passed: opens, pages, geometry, text_retained, not_rasterised |

_These checks do not change the scores; a failure is a warning next to them. Turning the page into a picture hides every value and also destroys the document, and these checks are what catch that._

### Where the leaks are

| Layer | Severity | Found in | Only here | Unread | Who can see it |
|:---|:---|---:|---:|---:|:---|
| rendered_pixels | critical | 0.222 | 0.111 | 0 | visible to anyone who opens the file |
| content_stream | critical | 0.037 | 0.000 | 0 | copy-paste, pdftotext, any parser |
| ocr | high | 0.093 | 0.000 | 0 | catches translucent or hairline covers |

_Where in the file a leaked value was found. `Found in` is the share of sensitive values recoverable from that layer; `Only here` is the share no other layer revealed; `Unread` counts probes the layer could not be checked for. Nothing leaked through: prior_revision, image_xobject, metadata, annotation, attachment, optional_content, font_subset._

### Leak rate by category

| Category | Leak rate | Over-redaction | Probes |
|:---|---:|---:|---:|
| PERSON | 0.222 [0.13, 0.35] n=54 | n/a | 54 |

_One overall number can hide a category the tool never handles, so each is shown on its own. Look for rows near 1.000._

### What was not measured

| Kind | Statement |
|:---|:---|
| tool does not attempt | does not perform: detection, extraction |

_Probes excluded here are not in the rates above - which is not the same as passing them. A tool that skips handwriting will leave handwritten details on a real document._

## Details

_Finer breakdowns and the record of what produced the numbers. Not needed to understand the result._

### Leak rate by severity

| Severity | Leak rate | Over-redaction | Probes |
|:---|---:|---:|---:|
| high | 0.222 [0.13, 0.35] n=54 | n/a | 54 |

_Each level up counts double in the weighted leak rate, so one critical leak outweighs several low ones._

### Leak rate by difficulty tier

| Difficulty | Leak rate | Over-redaction | Probes |
|:---|---:|---:|---:|
| easy | 0.000 [0.00, 0.79] n=1 | n/a | 1 |
| hard | 0.226 [0.13, 0.36] n=53 | n/a | 53 |

_How the tool copes as values get harder to spot. A tool at 0.9 easy / 0.3 hard is a different product from a uniform 0.6._

### Leak rate by structural trap

| Trap | Leak rate | Over-redaction | Probes |
|:---|---:|---:|---:|
| none | 0.222 [0.13, 0.35] n=54 | n/a | 54 |

_Values placed to trip up tools that only read the text layer, however the page looks._

### Channel reach

| Channel | Reach |
|:---|---:|
| image_text | 0.704 [0.52, 0.84] n=27 |
| text_layer | 0.852 [0.68, 0.94] n=27 |

_Share of values redacted that could only be found through that channel (text layer, image, metadata...). 0 means the tool does not read that channel at all. 1 is best._

### Cost of each rendering condition

| Axis | Level | Delta vs control |
|:---|:---|---:|
| orientation | vertical | 0.806 |
| orientation | rot180 | 0.056 |
| orientation | rot270 | 0.056 |
| orientation | horizontal | 0.000 |
| orientation | rot90 | -0.194 |
| orientation | skew | -0.194 |
| polarity | screened | 0.446 |
| polarity | on-image | 0.346 |
| polarity | highlighted | 0.046 |
| polarity | normal | 0.000 |
| polarity | inverse | -0.011 |
| polarity | low-contrast | -0.154 |
| provenance | hand-block | 0.252 |
| provenance | hand-cursive | 0.252 |
| provenance | hand-mixed | 0.185 |
| provenance | print-degraded | 0.074 |
| provenance | print-clean | 0.052 |
| provenance | vector | 0.000 |
| scale | pt10 | 0.000 |
| scale | pt6 | -0.068 |
| scale | pt4 | -0.250 |

_How much worse the tool does under each condition than on plain, upright text. 0 means no extra cost; 0.6 means it removes 60 points fewer values._

### Detection

| Run | Mode | Recall | Precision | Type accuracy | Note |
|:---|:---|---:|---:|---:|:---|
| this run | inferred | - | - | - | the tool reported no entity list, so detection is unidentifiable: a detection miss and a redaction failure are the same observation. Redaction outcomes are reported instead. |

_Only measurable when the tool reports what it found. When it does not (`inferred`), a value it never spotted and one it spotted but failed to remove look the same, so no separate detection score is given._

### Provenance

| Run | Field | Value |
|:---|:---|:---|
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | tool | pdf-redaction:web |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | observed_at | 2026-09-25T08:14:47+00:00 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | attempt | 1 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | transport | manual |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | case | extraction-conditions-1 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | dataset revision | v0.1.1 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | output sha256 | 4c271feece6f5443 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | engine ocr | tesseract |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | engine pdfium | 153.0.7999.0 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | engine pypdf | 6.19.0 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | engine pypdfium2 | 5.13.0 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | engine renderer | pypdfium2 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | engine tesseract | tesseract 5.5.2 |
| 20260925T081446-pdf-redaction-web-extraction-conditions-1-a1-21dbff | alignment | fiducial · residual 0.348px |
| thresholds | beta | 2.0 |
| thresholds | delta_px | 24 |
| thresholds | disclosure_length | {'ACCOUNT': 4, 'CARD': 4, 'EMAIL': 4, 'IBAN': 4, 'NATIONAL_ID': 4, 'PASSPORT': 4, 'PHONE': 4, 'PLATE': 4, 'POSTCODE': 4} |
| thresholds | dpi | 300 |
| thresholds | geometry_tolerance | 0.01 |
| thresholds | glyph_floor | 0.25 |
| thresholds | glyph_legible | 0.5 |
| thresholds | glyph_merge_pt | 0.5 |
| thresholds | glyph_window_pt | 2.0 |
| thresholds | l_min | 4 |
| thresholds | locate_margin_pt | 2.0 |
| thresholds | max_residual_px | 6.0 |
| thresholds | min_char_survival | 0.05 |
| thresholds | min_ink_fraction | 0.01 |
| thresholds | min_text_retention | 0.9 |
| thresholds | morph_radius | 1 |
| thresholds | page_rewritten_fraction | 0.5 |
| thresholds | spill_epsilon_pt | 12.0 |
| thresholds | tau_cov | 0.98 |
| thresholds | tau_hash | 10 |
| thresholds | tau_iou | 0.5 |
| thresholds | tau_reveal | 0.02 |
| thresholds | tau_span | 0.5 |
| thresholds | tau_text | 0.5 |
| thresholds | z | 1.96 |

_What produced these numbers: tool, date, dataset revision, software versions and scoring thresholds. Scores are only comparable when the dataset revision and thresholds match._
