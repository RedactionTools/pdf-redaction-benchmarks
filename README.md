<div align="center">

<a href="https://redaction-tools.com">
  <img src="https://raw.githubusercontent.com/RedactionTools/pdf-redaction-benchmarks/main/assets/redaction-tools-logo.png" alt="Redaction Tools" width="104">
</a>

# pdfredeval

**An evaluation framework for third-party PDF redaction tools**

[redaction-tools.com](https://redaction-tools.com) ·
[github.com/RedactionTools/pdf-redaction-benchmarks](https://github.com/RedactionTools/pdf-redaction-benchmarks)

</div>

---

Public web services, desktop software and vendor APIs, measured as **black boxes**: no
source, no internals, only what a submitted page comes back looking like.

Part of [Redaction Tools](https://redaction-tools.com) — results are published to the
catalogue there.

Design, metric definitions and dataset structure live in [`docs/`](https://github.com/RedactionTools/pdf-redaction-benchmarks/blob/main/docs/README.md).

## Install

```sh
pip install pdfredeval                    # CLI + Python API: generation and adapters
pip install "pdfredeval[score,report]"    # + scoring and HTML report charts
```

Extras: `score`, `report`, `generate` (rasterised conditions), `datasets`, `publish`.

## Setup

For development, the project is managed with [uv](https://docs.astral.sh/uv/).

```sh
uv sync                    # create .venv, install the project editable + dev tools
uv sync --extra generate   # + Pillow, for the rasterised probe conditions
uv run pytest              # 300 tests
uv run ruff check .
uv run mypy
```

Scoring needs a PDF parser and a rasteriser — `pypdf`, `pypdfium2`, `pandas`, `numpy` —
and the report's charts need `matplotlib`. The `score` and `report` extras declare them
and the dev group pulls both in, so a plain `uv sync` can run the whole suite. Generation and the adapter layer still need nothing. The OCR leak layer
shells out to **tesseract** if it is on `PATH`; without it that layer is reported
`unavailable` rather than clean, because "found nothing" from a pass that never ran is the
same sentence as "no leak".

`uv run <cmd>` syncs first, so a fresh clone needs no separate install step. `uv.lock` is
committed: this project's whole premise is that a result names what produced it, and a
benchmark whose own toolchain drifts cannot hold tools to that standard.

## Try it

```sh
uv run pdfredeval families
uv run pdfredeval generate all --seed 1-3          # or one family: pii-detection
uv run pdfredeval inspect pii-detection-1 --probes
```

With no output flags, everything lands in one committed tree per package version (see
[Benchmark data](#benchmark-data)). `generate` writes
`benchmarks/v<version>/cases/<family>/<case_id>/{<case_id>.pdf, ground_truth.json}`, and
stamps the version folder as the case's dataset revision. The same seed **and
timestamp** reproduce byte-identical output on any machine — every page is stamped with
its generation time, and `ground_truth.json` records it so `--generated-at` replays a case
exactly. The full condition matrix (23 cells)
needs the `generate` extra for its rasterised cells; without it those are reported on
stderr as skipped, never silently omitted.

Driving a tool works best with a registered adapter, but none ship in-tree. For a tool
without one, `submit` lays out a manual run: the folder, `screenshots/`, a `TASK.md` for
the operator, and the `handle.json` that `collect` needs. It claims
every channel and rendering condition, so no probe is excused as unsupported:

```sh
uv run pdfredeval tools
uv run pdfredeval submit acme:web pii-detection-1 --operator you
#   ... follow <run_dir>/TASK.md, drop the tool's PDF into <run_dir>, any name ...
uv run pdfredeval collect <run_dir>
uv run pdfredeval score                  # every run of this version, then its reports
```

`score` also takes a bare PDF with `--case-dir`, which needs no adapter at all — and
scoring a case against *itself* is the do-nothing tool, so it must report a leak rate of 1:

```sh
uv run pdfredeval score some/dir/pii-detection-1.pdf \
    --case-dir some/dir
```

It writes `score/{probes.csv, report.json}`: one row per probe, and a report carrying the
threshold table, the engine versions and the alignment it was computed with. Every rate
comes with a Wilson interval and its `n`, because 0 leaks in 40 probes is `[0, 0.088]`,
not 100%.

`score` then reports on what it wrote: a summary in the terminal, `report.md` and a
**self-contained** `report.html` — no CDN, no sibling `charts/` directory, so it survives
being forwarded — beside a single run; for a sweep of the tree, under its `reports/` per
tool, per tool and family, and across tools (`--report-out`, or `./report`, for runs
outside the tree). Each report also draws ground truth over the tool's own
output (leaks boxed red, redactions green) unless you pass `--no-overlay`; `--no-report`
stops after the scored artefacts:

```sh
uv run pdfredeval score <run_dir> --no-overlay       # numbers only, no images
uv run pdfredeval score some/runs/ --title "October sweep"     # pooled into ./report
```

A target that already carries `score/` is reported from those artefacts, never re-scored —
a published number must not move because tesseract or pypdf drifted underneath it. Pass
`--rescore` to score such runs again; it asks first. Counts pool across runs; per-run
rates are never averaged.

| Command | |
|---|---|
| `families` | list case families |
| `generate` | write cases for a family or `all`; `--seed` takes `7`, `1,2,3` or `1-10` |
| `inspect` | summarise a case, `--probes` for the full list |
| `tools` | list registered adapters |
| `submit` | start a run; on the manual path writes a task for the operator |
| `collect` | collect a delivered run and write its manifest |
| `score` | align, probe and score a run — or any PDF against its case — then report on it |
| `login` / `logout` | sign this machine in to redaction-tools.com in the browser, or revoke its key |
| `publish` | send scored runs to [redaction-tools.com](https://redaction-tools.com/benchmarks) for rescoring and review |
| `publish-cases` | send cases and their ground truth to the site (staff keys only) |

### Publishing

Sign in once per machine. Your browser opens on redaction-tools.com; sign in there and
approve the code your terminal shows:

```sh
uv run pdfredeval login                         # --no-browser over SSH: prints the link
uv run pdfredeval publish <run_dir> --dry-run   # check, list, send nothing
uv run pdfredeval publish <run_dir> --notes "Pro plan, default settings"
uv run pdfredeval logout                        # revokes this machine's key
```

The key is saved in `~/.config/pdfredeval/credentials.json` (readable by you only;
`$PDFREDEVAL_CONFIG_DIR` moves it). For CI, where there is no browser, create a key on
your [account page](https://redaction-tools.com/account) and set
`PDFREDEVAL_API_KEY=<prefix>.<secret>` instead: the environment is read only, never a
flag, and it wins over a saved login.

Runs are grouped into one submission per tool and dataset revision. Each run sends its
`manifest.json`, `score/report.json`, the delivered PDF and its overlay, and nothing else:
never `ground_truth.json`, `probes.csv`, screenshots or `entities.json`. The site rescores
every PDF with the same scorer version and badges the result *verified* or *disputed*. An
editor reviews it before it reaches the leaderboard. `--site` or `$PDFREDEVAL_SITE`
points at another deployment, for example `http://localhost:8007`.

## Benchmark data

Generated cases, tool runs, scores and reports are committed, one tree per package
version, because a score is only comparable against the exact cases it was run on:

```
benchmarks/
  LATEST                                    newest version folder
  v0.1.1/
    README.md                               index: cases per family, runs per tool, reports
    cases/<family>/<case_id>/               <case_id>.pdf, ground_truth.json
    runs/<vendor-surface>/<run_id>/         manifest.json, the tool's PDF (any name),
                                            TASK.md, screenshots/, score/, report/
    reports/<vendor-surface>/               one tool, every family
    reports/<vendor-surface>/<family>/      one tool, one family
    reports/_summary/[<family>/]            every tool
```

Case PDFs are named by case id, so the family is in the name: a PDF uploaded to a vendor
or attached to a ticket still says what it is. A run's output keeps whatever name the
tool gave it - the run folder already names the tool - and `collect` records that name
in the manifest. Keep one PDF per run folder; with two, `collect` asks which.

- `--bench-root` (or `$PDFREDEVAL_HOME`) moves the tree; `--bench-version` picks another
  version folder. `-o`, `--runs-dir` and `--out` still write anywhere else.
- Nothing already there is overwritten unasked. At a terminal you are asked to overwrite,
  skip or abort; elsewhere (CI, pipes) the command stops unless you pass `--force` or
  `--skip-existing`. Reports are derived from `score/` and are simply redrawn.
- Trees and runs from before this layout still load: a case's `case.pdf` and a run's
  `output.pdf` are read under their old names.

## Layout

| Path | |
|---|---|
| `docs/` | the design brief — read [`docs/README.md`](https://github.com/RedactionTools/pdf-redaction-benchmarks/blob/main/docs/README.md) first |
| `src/pdfredeval/generate/` | seeded case generation: values, layout, PDF writer, families |
| `src/pdfredeval/tools/` | the `Tool` contract, vendor registry, manual and API transports |
| `src/pdfredeval/align.py` | fiducials to ground-truth space, and when not to trust the mapping |
| `src/pdfredeval/probe/` | one module per leak surface: stream, annotations, hidden layers, revisions, metadata, images, pixels, OCR |
| `src/pdfredeval/score/` | the removal rules, one row per probe, and every rate over them |
| `src/pdfredeval/report/` | tables, charts, the overlay images, and the page they go on |
| `tests/` | contract and invariant tests, plus fake tools with known answers |
| `assets/` | the Redaction Tools mark, also embedded in every generated page |

## Status

Implemented: case generation, the adapter layer, the aligner, the probers, the scorer, the
reporter and the publisher.

Not yet: object probes (faces, signatures, QR codes, stamps), which leaves
the image, attachment and font-subset leak layers implemented but with nothing to catch;
and the raster-dependent rendering conditions (handwriting, photo grounds, degraded
scans), which are reported as skipped rather than silently omitted.
