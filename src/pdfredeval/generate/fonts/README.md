# Bundled fonts

Rasterised conditions (`print-*`, `hand-*`) need glyphs drawn to pixels. These faces are
committed rather than discovered on the host so that a seeded case is byte-identical on
every machine — the property the whole benchmark rests on. A system font would make the
same case id produce different bytes on macOS and on Linux CI.

| File | Used for | Licence |
|---|---|---|
| `RobotoMono.ttf` | `print-clean`, `print-degraded` | OFL 1.1 — `LICENSE-RobotoMono.txt` |
| `Caveat.ttf` | `hand-block` (unjoined handwriting) | OFL 1.1 — `LICENSE-Caveat.txt` |
| `DancingScript.ttf` | `hand-cursive`, `hand-mixed` (joined script) | OFL 1.1 — `LICENSE-DancingScript.txt` |

All three are SIL Open Font License 1.1, which permits redistribution. Each font's
SHA-256 is recorded in the ground truth of every case that uses it, so a disputed result
names the exact glyphs it was produced from.

**Fidelity caveat.** Font-rendered handwriting is more regular than real handwriting, so
`hand-*` probes are a *floor* on difficulty, not a substitute for samples of real writing.
Reported as such — see `docs/metrics/extraction.md`.
