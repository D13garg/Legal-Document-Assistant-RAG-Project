"""Deterministic metrics over (case, observation) (ADR-018). Every score is in [0, 1], higher is
better.

A metric `applies` to the cases it can judge; the others are not counted. Scores come from
identifiers
and outcomes only, never from text. An errored observation scores 0.0 on every applicable metric
(fail closed): a crash is never a good result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lda.modules.evaluation.domain.dataset import EvaluationCase
from lda.modules.evaluation.domain.observation import Observation
from lda.modules.evaluation.domain.outcome import Outcome
from lda.modules.evaluation.domain.values import label

OUTCOME_ACCURACY = "outcome_accuracy"
RETRIEVAL_RECALL = "retrieval_recall"
CITATION_PRECISION = "citation_precision"
CITATION_RECALL = "citation_recall"
ISOLATION = "isolation"


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    applies: Callable[[EvaluationCase], bool]
    score: Callable[[EvaluationCase, Observation], float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", label(self.name, "metric"))

    def score_case(self, case: EvaluationCase, observation: Observation) -> float:
        if observation.outcome is Outcome.ERROR:
            return 0.0
        return min(1.0, max(0.0, float(self.score(case, observation))))


def _expects_answer(case: EvaluationCase) -> bool:
    return case.expected_outcome is Outcome.ANSWERED


def _always(case: EvaluationCase) -> bool:
    return True


def _has_forbidden(case: EvaluationCase) -> bool:
    return bool(case.forbidden_source_ids)


def _outcome_accuracy(case: EvaluationCase, obs: Observation) -> float:
    return 1.0 if obs.outcome is case.expected_outcome else 0.0


def _retrieval_recall(case: EvaluationCase, obs: Observation) -> float:
    expected = set(case.expected_source_ids)
    return len(expected & set(obs.retrieved_source_ids)) / len(expected)


def _citation_precision(case: EvaluationCase, obs: Observation) -> float:
    # An answer that cites nothing is unsupported, so it scores 0.0 rather than being skipped.
    if obs.outcome is not Outcome.ANSWERED or not obs.cited_source_ids:
        return 0.0
    return len(set(case.expected_source_ids) & set(obs.cited_source_ids)) / len(
        obs.cited_source_ids
    )


def _citation_recall(case: EvaluationCase, obs: Observation) -> float:
    if obs.outcome is not Outcome.ANSWERED:
        return 0.0
    expected = set(case.expected_source_ids)
    return len(expected & set(obs.cited_source_ids)) / len(expected)


def _isolation(case: EvaluationCase, obs: Observation) -> float:
    seen = set(obs.retrieved_source_ids) | set(obs.cited_source_ids)
    return 0.0 if seen & set(case.forbidden_source_ids) else 1.0


def default_metrics() -> tuple[Metric, ...]:
    return (
        Metric(OUTCOME_ACCURACY, _always, _outcome_accuracy),
        Metric(RETRIEVAL_RECALL, _expects_answer, _retrieval_recall),
        Metric(CITATION_PRECISION, _expects_answer, _citation_precision),
        Metric(CITATION_RECALL, _expects_answer, _citation_recall),
        Metric(ISOLATION, _has_forbidden, _isolation),
    )
