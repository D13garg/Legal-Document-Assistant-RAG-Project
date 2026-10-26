"""Evaluation wiring (ADR-018): joins the pure runner to its settings and an injected target.

Nothing is persisted, cached or tenant-dependent and no provider is called. The target (the real
system
under test) is supplied by the caller; the composition of a real target belongs to a later step.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.evaluation.api import (
    EvaluationLimits,
    EvaluationTarget,
    RunEvaluation,
    default_metrics,
)
from lda.modules.evaluation.config import EvaluationSettings
from lda.platform.configuration.loader import ConfigurationLoader


@dataclass(frozen=True)
class EvaluationContainer:
    run_evaluation: RunEvaluation


def load_evaluation_settings(config: ConfigurationLoader) -> EvaluationSettings:
    return config.load("evaluation", EvaluationSettings)


def build_evaluation(settings: EvaluationSettings, target: EvaluationTarget) -> EvaluationContainer:
    limits = EvaluationLimits(settings.max_cases, settings.max_question_characters)
    return EvaluationContainer(RunEvaluation(target, default_metrics(), limits))
