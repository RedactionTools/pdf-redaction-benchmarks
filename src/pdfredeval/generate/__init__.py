"""Case generation: seeded, procedural, ground truth beside every PDF."""

from .builder import GENERATOR_VERSION, CaseBuilder, UnsupportedCondition
from .families import (
    CONDITION_CELLS,
    FAMILIES,
    INTERACTION_CELLS,
    OFAT_CELLS,
    Generated,
    Skipped,
    generate,
    generate_split,
)
from .layout import Fiducial, PageLayout, Slot
from .pdf import A4, COURIER, Font, Matrix, PdfWriter, check_xref
from .values import Collision, PoolExhausted, Value, ValueFactory

__all__ = [
    "A4", "COURIER", "CONDITION_CELLS", "Collision", "CaseBuilder", "FAMILIES",
    "Fiducial", "Font", "GENERATOR_VERSION", "Generated", "INTERACTION_CELLS",
    "Matrix", "OFAT_CELLS", "PageLayout", "PdfWriter", "PoolExhausted", "Skipped",
    "Slot", "UnsupportedCondition", "Value", "ValueFactory", "check_xref", "generate",
    "generate_split",
]
