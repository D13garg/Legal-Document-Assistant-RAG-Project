"""Input of a run (ADR-018). The report type is public domain data (`domain.report`)."""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.evaluation.domain.configuration import RagConfiguration
from lda.modules.evaluation.domain.dataset import EvaluationDataset
from lda.modules.evaluation.domain.gate import GatePolicy


@dataclass(frozen=True, slots=True)
class RunEvaluationCommand:
    """One explicit dataset, the configuration under test and the gate it must pass."""

    dataset: EvaluationDataset
    configuration: RagConfiguration
    policy: GatePolicy
