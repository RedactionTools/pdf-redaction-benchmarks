"""A case with one value planted on every leak surface.

No shipped family plants structural traps at the moment, but the prober and scorer
still read every surface - so the tests that defend them build their own case here.
"""

from __future__ import annotations

from pathlib import Path

from pdfredeval.generate import CaseBuilder, PageLayout, ValueFactory
from pdfredeval.types import Case

#: (builder method, value kind), one row each, top to bottom.
SURFACES: tuple[tuple[str, str], ...] = (
    ("add_text_probe", "person"),
    ("add_invisible_probe", "email"),
    ("add_annotation_probe", "phone"),
    ("add_hidden_layer_probe", "account"),
    ("add_prior_revision_probe", "national_id"),
)


def leak_surface_case(out_dir: Path | str, *, seed: int) -> Case:
    case_id = f"leak-surfaces-{seed}"
    b = CaseBuilder(case_id, seed, "leak-surfaces", PageLayout())
    f = ValueFactory(seed)
    b.reserve_with(f)
    for text in b.brand_strings:
        f.reserve_text(text)
    slots = b.layout.grid(len(SURFACES) * 2, top=40.0)
    for method, kind in SURFACES:
        getattr(b, method)(f.target(kind), next(slots))
        next(slots)  # a blank row between surfaces, so no box reaches the next caption
    b.add_metadata_probe(f.target("dob"), key="Keywords")
    b.add_metadata_probe(f.target("address"), key="ClientRecord", xmp=True)
    return b.emit(out_dir)
