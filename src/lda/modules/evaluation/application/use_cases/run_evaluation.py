"""`RunEvaluation`: run a dataset against a target, score it and apply the release gate (ADR-018,
ADR-030).
"""

from __future__ import annotations

import math

from lda.modules.evaluation.application.dto import RunEvaluationCommand
from lda.modules.evaluation.application.limits import EvaluationLimits
from lda.modules.evaluation.application.ports import EvaluationTarget
from lda.modules.evaluation.domain.codes import (
    CASE_ERROR,
    INVALID_INPUT,
    LIMIT_EXCEEDED,
    OBSERVATION_INVALID,
)
from lda.modules.evaluation.domain.dataset import EvaluationCase
from lda.modules.evaluation.domain.gate import decide
from lda.modules.evaluation.domain.metrics import Metric
from lda.modules.evaluation.domain.observation import Observation
from lda.modules.evaluation.domain.outcome import Outcome
from lda.modules.evaluation.domain.report import CaseResult, EvaluationReport, MetricResult
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import ValidationError


@public(reason="Offline run over synthetic datasets; no tenant, principal or customer data.")
@not_audited(reason="Writes nothing and changes no state; the report is the result (ADR-018).")
class RunEvaluation:
    """Deterministic for a deterministic target: no clock, no randomness, no persistence and no
    provider call."""

    def __init__(
        self, target: EvaluationTarget, metrics: tuple[Metric, ...], limits: EvaluationLimits
    ) -> None:
        names = [m.name for m in metrics]
        if not metrics or len(set(names)) != len(names):
            raise ValidationError(INVALID_INPUT, detail="metrics")
        self._target = target
        self._metrics = metrics
        self._limits = limits

    def execute(self, command: RunEvaluationCommand) -> EvaluationReport:
        self._check(command)
        cases = command.dataset.cases
        results: list[CaseResult] = []
        sums: dict[str, list[float]] = {m.name: [] for m in self._metrics}
        errors = 0
        for case in cases:
            observation, error_code = self._observe(case)
            errors += observation.outcome is Outcome.ERROR
            scores = tuple(
                (m.name, m.score_case(case, observation)) for m in self._metrics if m.applies(case)
            )
            for name, score in scores:
                sums[name].append(score)
            results.append(CaseResult(case.case_id, observation.outcome, scores, error_code))
        metric_results = tuple(
            MetricResult(name, len(values), math.fsum(values) / len(values))
            for name, values in sums.items()
            if values
        )
        decision = decide(
            command.policy, len(cases), errors, {r.metric: r.mean for r in metric_results}
        )
        return EvaluationReport(
            config_id=command.configuration.config_id,
            dataset_id=command.dataset.dataset_id,
            dataset_version=command.dataset.version,
            dataset_fingerprint=command.dataset.fingerprint(),
            case_count=len(cases),
            error_count=errors,
            metrics=metric_results,
            cases=tuple(results),
            decision=decision,
        )

    def _check(self, command: RunEvaluationCommand) -> None:
        if len(command.dataset.cases) > self._limits.max_cases:
            raise ValidationError(LIMIT_EXCEEDED, detail="max_cases")
        if any(
            len(c.question.reveal()) > self._limits.max_question_characters
            for c in command.dataset.cases
        ):
            raise ValidationError(LIMIT_EXCEEDED, detail="max_question_characters")
        known = {m.name for m in self._metrics}
        if any(t.metric not in known for t in command.policy.thresholds):
            raise ValidationError(INVALID_INPUT, detail="thresholds")

    def _observe(self, case: EvaluationCase) -> tuple[Observation, str]:
        """A target failure or malformed answer is one errored case; it never aborts the
        run, and the
        exception, which could carry content, is discarded."""
        try:
            observation = self._target.observe(case)
        except Exception:
            return Observation.errored(), CASE_ERROR
        if not isinstance(observation, Observation):
            return Observation.errored(), OBSERVATION_INVALID
        return observation, ""
