"""The result of one evaluation run (ADR-018): identifiers, counts and scores only; no text, no
timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.evaluation.domain.gate import GateDecision
from lda.modules.evaluation.domain.outcome import Outcome


@dataclass(frozen=True, slots=True)
class MetricResult:
    metric: str
    cases_measured: int
    mean: float


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    outcome: Outcome
    scores: tuple[tuple[str, float], ...]  # (metric, score) for the metrics that applied
    error_code: str = ""  # a stable code when the case errored; never a message


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    config_id: str
    dataset_id: str
    dataset_version: int
    dataset_fingerprint: str
    case_count: int
    error_count: int
    metrics: tuple[MetricResult, ...]
    cases: tuple[CaseResult, ...]
    decision: GateDecision
