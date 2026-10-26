"""Results of the release-gating use cases (ADR-041). Identifiers, hashes and codes only."""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.evaluation.domain.release import EligibilityDecision


@dataclass(frozen=True, slots=True)
class EvaluationRunResult:
    run_id: str
    config_id: str
    passed: bool
    run_sha256: str
    failures: tuple[tuple[str, str], ...]  # (gate failure code, subject)


@dataclass(frozen=True, slots=True)
class EligibilityOutcome:
    decision: EligibilityDecision
    eligibility_id: str | None
    replayed: bool
