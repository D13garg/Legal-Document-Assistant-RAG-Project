"""Release-gate policy and decision (ADR-030). Pure and fail closed.

A gate passes only when every named criterion holds. An empty policy is rejected (nothing to
pass), a
metric the run did not measure fails its threshold, and a run with too few cases or too many
errors fails.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.evaluation.domain.codes import (
    GATE_ERROR_RATE_EXCEEDED,
    GATE_METRIC_BELOW_MINIMUM,
    GATE_METRIC_NOT_MEASURED,
    GATE_TOO_FEW_CASES,
    INVALID_INPUT,
)
from lda.modules.evaluation.domain.values import label, positive_int, unit_interval
from lda.shared.kernel.errors import ValidationError

MAX_THRESHOLDS = 64


@dataclass(frozen=True, slots=True)
class MetricThreshold:
    metric: str
    minimum: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric", label(self.metric, "metric"))
        object.__setattr__(self, "minimum", unit_interval(self.minimum, "minimum"))


@dataclass(frozen=True, slots=True)
class GatePolicy:
    thresholds: tuple[MetricThreshold, ...]
    min_cases: int = 1
    max_error_rate: float = 0.0

    def __post_init__(self) -> None:
        if not 1 <= len(self.thresholds) <= MAX_THRESHOLDS:
            raise ValidationError(INVALID_INPUT, detail="thresholds")
        names = [t.metric for t in self.thresholds]
        if len(set(names)) != len(names):
            raise ValidationError(INVALID_INPUT, detail="thresholds")
        positive_int(self.min_cases, "min_cases")
        unit_interval(self.max_error_rate, "max_error_rate")


@dataclass(frozen=True, slots=True)
class GateFailure:
    code: str
    subject: str  # a metric name, or "run"


@dataclass(frozen=True, slots=True)
class GateDecision:
    passed: bool
    failures: tuple[GateFailure, ...]


def decide(
    policy: GatePolicy, case_count: int, error_count: int, means: dict[str, float]
) -> GateDecision:
    """`means` holds only metrics measured on at least one case."""
    failures: list[GateFailure] = []
    if case_count < policy.min_cases:
        failures.append(GateFailure(GATE_TOO_FEW_CASES, "run"))
    if case_count == 0 or error_count / case_count > policy.max_error_rate:
        failures.append(GateFailure(GATE_ERROR_RATE_EXCEEDED, "run"))
    for threshold in policy.thresholds:
        mean = means.get(threshold.metric)
        if mean is None:
            failures.append(GateFailure(GATE_METRIC_NOT_MEASURED, threshold.metric))
        elif mean < threshold.minimum:
            failures.append(GateFailure(GATE_METRIC_BELOW_MINIMUM, threshold.metric))
    return GateDecision(not failures, tuple(failures))
