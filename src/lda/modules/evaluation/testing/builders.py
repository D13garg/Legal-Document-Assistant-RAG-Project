"""Test-only builders and fake targets for evaluation and its consumers (never imported by
production code).
"""

from __future__ import annotations

from collections.abc import Mapping

from lda.modules.evaluation.application.limits import EvaluationLimits
from lda.modules.evaluation.domain.configuration import RagConfiguration
from lda.modules.evaluation.domain.dataset import EvaluationCase, EvaluationDataset
from lda.modules.evaluation.domain.observation import Observation
from lda.modules.evaluation.domain.outcome import Outcome
from lda.shared.kernel.sensitive import Sensitive


def build_case(
    case_id: str = "case-1",
    *,
    outcome: Outcome = Outcome.ANSWERED,
    expected: tuple[str, ...] | None = None,
    forbidden: tuple[str, ...] = (),
    question: str = "synthetic question",
) -> EvaluationCase:
    if expected is None:
        expected = ("src-1",) if outcome is Outcome.ANSWERED else ()
    return EvaluationCase(case_id, Sensitive(question), outcome, expected, forbidden)


def build_dataset(
    *cases: EvaluationCase, dataset_id: str = "synthetic", version: int = 1
) -> EvaluationDataset:
    return EvaluationDataset(dataset_id, version, cases or (build_case(),))


def build_configuration(**overrides: Mapping[str, str | int | float | bool]) -> RagConfiguration:
    components: dict[str, Mapping[str, str | int | float | bool]] = {
        "retrieval": {"top_k": 8},
        "generation": {"model": "fake-model"},
    }
    components.update(overrides)
    return RagConfiguration.create(components)


def default_limits() -> EvaluationLimits:
    return EvaluationLimits(max_cases=1_000, max_question_characters=8_000)


class PerfectTarget:
    """Returns exactly what each case expects."""

    def observe(self, case: EvaluationCase) -> Observation:
        if case.expected_outcome is Outcome.ANSWERED:
            return Observation(Outcome.ANSWERED, case.expected_source_ids, case.expected_source_ids)
        return Observation(Outcome.INSUFFICIENT_EVIDENCE)


class ScriptedTarget:
    """Answers from a script keyed by case id; a value that is an exception is raised, and a
    case missing
    from the script raises. Records the ids it was asked about, never the questions."""

    def __init__(self, script: Mapping[str, Observation | BaseException | object]) -> None:
        self._script = dict(script)
        self.asked: list[str] = []

    def observe(self, case: EvaluationCase) -> Observation:
        self.asked.append(case.case_id)
        result = self._script[case.case_id]
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]  # tests may script a malformed answer on purpose
