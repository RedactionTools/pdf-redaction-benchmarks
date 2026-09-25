"""Scoring: probe rows, the rates over them, and the record that gets published."""

from .detection import DetectionReport, Entity, load_entities
from .metrics import Rate, pooled_leak_rate, rate, wilson
from .rows import Outcome, ProbeRow, to_frame
from .rules import LayerResult, Verdict, evaluate
from .scorer import (
    REPORT_NAME,
    ROWS_NAME,
    SCORE_DIR,
    ScoreResult,
    ScoringError,
    score,
    score_run,
)

__all__ = [
    "REPORT_NAME",
    "ROWS_NAME",
    "SCORE_DIR",
    "DetectionReport",
    "Entity",
    "LayerResult",
    "Outcome",
    "ProbeRow",
    "Rate",
    "ScoreResult",
    "ScoringError",
    "Verdict",
    "evaluate",
    "load_entities",
    "pooled_leak_rate",
    "rate",
    "score",
    "score_run",
    "to_frame",
    "wilson",
]
