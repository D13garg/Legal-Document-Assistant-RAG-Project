"""Release-gating wiring (ADR-041): joins the evaluation use cases to a store, a clock and the
evaluation settings. Nothing here decides anything and nothing deploys anything.

`assert_release_eligible` is the one place a start-up or release step should ask the question: it
fails closed, so an eligibility check that cannot be completed is a refusal, never a pass.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.bootstrap.rag_configuration import RuntimeConfiguration
from lda.modules.evaluation.api import (
    RELEASE_DATASET,
    CheckEligibility,
    EvaluateConfiguration,
    EvaluationLimits,
    EvaluationStore,
    EvaluationTarget,
    RecordEligibility,
    ReleaseCriteria,
    RunEvaluation,
    default_metrics,
    release_criteria,
)
from lda.modules.evaluation.config import EvaluationSettings
from lda.shared.kernel.errors import ConfigurationError, InfrastructureError
from lda.shared.ports.clock import Clock, IdGenerator

RELEASE_NOT_ELIGIBLE = "release_not_eligible"


@dataclass(frozen=True)
class ReleaseGatingContainer:
    evaluate: EvaluateConfiguration
    record_eligibility: RecordEligibility
    check_eligibility: CheckEligibility
    criteria: ReleaseCriteria


def build_release_gating(
    settings: EvaluationSettings,
    target: EvaluationTarget,
    store: EvaluationStore,
    clock: Clock,
    ids: IdGenerator,
) -> ReleaseGatingContainer:
    criteria = release_criteria(settings.max_evidence_age_seconds)
    runner = RunEvaluation(
        target,
        default_metrics(),
        EvaluationLimits(settings.max_cases, settings.max_question_characters),
    )
    return ReleaseGatingContainer(
        EvaluateConfiguration(runner, RELEASE_DATASET, criteria, store, clock, ids),
        RecordEligibility(criteria, store, clock, ids),
        CheckEligibility(criteria, store, clock),
        criteria,
    )


def assert_release_eligible(gating: ReleaseGatingContainer, runtime: RuntimeConfiguration) -> None:
    """Raise unless the running configuration is eligible right now. Codes only, no values."""
    try:
        decision = gating.check_eligibility.execute(runtime.effective)
    except InfrastructureError:
        raise ConfigurationError(RELEASE_NOT_ELIGIBLE, detail="check_failed") from None
    if not decision.eligible:
        raise ConfigurationError(RELEASE_NOT_ELIGIBLE, detail=",".join(decision.failures))
