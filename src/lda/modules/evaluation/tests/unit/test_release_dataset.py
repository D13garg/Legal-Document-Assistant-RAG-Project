"""ADR-041 D5: the approved dataset and policy are pinned; a change must be deliberate."""

from __future__ import annotations

from collections import Counter

from lda.modules.evaluation.api import (
    RELEASE_DATASET,
    RELEASE_GATE_POLICY,
    Outcome,
    RunEvaluation,
    RunEvaluationCommand,  # noqa: F401  (public-surface check)
    default_metrics,
    release_criteria,
)
from lda.modules.evaluation.application.dto import RunEvaluationCommand as Command
from lda.modules.evaluation.testing import PerfectTarget, build_effective, default_limits

PINNED_FINGERPRINT = "fb91a6010b43a0b7a3ba2e7a96ba53aa01c44546a29821d55f225901f53301f4"


def test_the_dataset_fingerprint_is_pinned() -> None:
    assert RELEASE_DATASET.fingerprint() == PINNED_FINGERPRINT, (
        "the approved dataset changed: re-approve it, update ADR-041 and this pin"
    )


def test_the_dataset_covers_every_reviewed_category() -> None:
    kinds = Counter(c.case_id.rsplit("-", 1)[0] for c in RELEASE_DATASET.cases)
    assert kinds == {
        "supported": 5,
        "multi": 2,
        "insufficient": 3,
        "isolation": 3,
        "injection": 1,
    }
    assert len(RELEASE_DATASET.cases) == 14
    assert any(c.expected_outcome is Outcome.INSUFFICIENT_EVIDENCE for c in RELEASE_DATASET.cases)
    assert sum(bool(c.forbidden_source_ids) for c in RELEASE_DATASET.cases) == 4


def test_the_policy_is_the_proposed_one_and_requires_every_case() -> None:
    thresholds = {t.metric: t.minimum for t in RELEASE_GATE_POLICY.thresholds}
    assert thresholds == {
        "outcome_accuracy": 0.95,
        "retrieval_recall": 0.90,
        "citation_precision": 0.95,
        "citation_recall": 0.90,
        "isolation": 1.0,
    }
    assert RELEASE_GATE_POLICY.min_cases == 14
    assert RELEASE_GATE_POLICY.max_error_rate == 0.0


def test_the_criteria_follow_the_dataset() -> None:
    criteria = release_criteria(3600)
    assert criteria.dataset_fingerprint == RELEASE_DATASET.fingerprint()
    assert criteria.case_ids == tuple(c.case_id for c in RELEASE_DATASET.cases)


def test_a_perfect_target_passes_the_release_policy() -> None:
    runner = RunEvaluation(PerfectTarget(), default_metrics(), default_limits())
    report = runner.execute(
        Command(RELEASE_DATASET, build_effective().configuration(), RELEASE_GATE_POLICY)
    )
    assert report.decision.passed
    assert report.case_count == 14 and report.error_count == 0
