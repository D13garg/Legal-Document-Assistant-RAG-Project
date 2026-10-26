"""ADR-041 D4: run-and-record, record-eligibility and check-eligibility, fail closed."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from lda.modules.evaluation.api import (
    ELIGIBILITY_CONFIG_MISMATCH,
    ELIGIBILITY_EVIDENCE_STALE,
    ELIGIBILITY_NO_EVIDENCE,
    ELIGIBILITY_NOT_RECORDED,
    ELIGIBILITY_RECENT_FAILURE,
    ELIGIBILITY_RECORD_CORRUPT,
    ELIGIBILITY_RUN_FAILED,
    ELIGIBILITY_SUPERSEDED,
    RELEASE_DATASET,
    CheckEligibility,
    EvaluateConfiguration,
    Observation,
    Outcome,
    RecordEligibility,
    RunEvaluation,
    default_metrics,
    release_criteria,
)
from lda.modules.evaluation.testing import (
    InMemoryEvaluationStore,
    PerfectTarget,
    ScriptedTarget,
    build_effective,
    default_limits,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConflictError, ValidationError

START = datetime(2026, 1, 1, 12, tzinfo=UTC)
MAX_AGE = 3_600
CRITERIA = release_criteria(MAX_AGE)


class Rig:
    def __init__(self, target: object | None = None) -> None:
        self.store = InMemoryEvaluationStore()
        self.clock = FakeClock(START)
        self.ids = SequentialIdGenerator()
        self.set_target(target or PerfectTarget())
        self.record = RecordEligibility(CRITERIA, self.store, self.clock, self.ids)
        self.check = CheckEligibility(CRITERIA, self.store, self.clock)

    def set_target(self, target: object) -> None:
        runner = RunEvaluation(target, default_metrics(), default_limits())  # type: ignore[arg-type]
        self.evaluate = EvaluateConfiguration(
            runner, RELEASE_DATASET, CRITERIA, self.store, self.clock, self.ids
        )


def failing_target() -> ScriptedTarget:
    return ScriptedTarget(
        {c.case_id: Observation(Outcome.INSUFFICIENT_EVIDENCE) for c in RELEASE_DATASET.cases}
    )


def test_the_full_path_makes_exactly_the_evaluated_configuration_eligible() -> None:
    rig, effective = Rig(), build_effective()
    result = rig.evaluate.execute(effective)
    assert result.passed and result.config_id == effective.config_id and result.failures == ()
    outcome = rig.record.execute(effective)
    assert outcome.decision.eligible and outcome.eligibility_id and not outcome.replayed
    assert rig.check.execute(effective).eligible


def test_nothing_is_eligible_without_evidence() -> None:
    rig = Rig()
    assert rig.check.execute(build_effective()).failures == (ELIGIBILITY_NO_EVIDENCE,)
    outcome = rig.record.execute(build_effective())
    assert not outcome.decision.eligible and rig.store.eligibility == []


def test_a_passing_run_alone_is_not_an_eligibility_record() -> None:
    rig, effective = Rig(), build_effective()
    rig.evaluate.execute(effective)
    assert rig.check.execute(effective).failures == (ELIGIBILITY_NOT_RECORDED,)


def test_a_failing_run_is_stored_as_evidence_but_never_makes_a_record() -> None:
    rig, effective = Rig(failing_target()), build_effective()
    result = rig.evaluate.execute(effective)
    assert not result.passed and result.failures
    assert len(rig.store.runs) == 1
    outcome = rig.record.execute(effective)
    assert not outcome.decision.eligible and ELIGIBILITY_RUN_FAILED in outcome.decision.failures
    assert rig.store.eligibility == []
    assert not rig.check.execute(effective).eligible


def test_another_configuration_cannot_use_this_configurations_evidence() -> None:
    rig = Rig()
    rig.evaluate.execute(build_effective())
    rig.record.execute(build_effective())
    other = build_effective(generation__model="other-model:2")
    assert rig.check.execute(other).failures == (ELIGIBILITY_NO_EVIDENCE,)
    assert not rig.record.execute(other).decision.eligible


def test_changing_a_setting_after_evaluation_invalidates_eligibility() -> None:
    rig = Rig()
    rig.evaluate.execute(build_effective())
    rig.record.execute(build_effective())
    for change in (
        {"retrieval__max_results": 19},
        {"generation__temperature": 0.1},
        {"chunking__max_characters": 1_999},
    ):
        assert not rig.check.execute(build_effective(**change)).eligible


def test_eligibility_expires_when_the_evidence_ages_out() -> None:
    rig, effective = Rig(), build_effective()
    rig.evaluate.execute(effective)
    rig.record.execute(effective)
    rig.clock.advance(timedelta(seconds=MAX_AGE))
    assert rig.check.execute(effective).eligible  # boundary is inclusive
    rig.clock.advance(timedelta(seconds=1))
    assert rig.check.execute(effective).failures == (ELIGIBILITY_EVIDENCE_STALE,)
    assert not rig.record.execute(effective).decision.eligible


def test_recording_twice_replays_and_writes_one_record() -> None:
    rig, effective = Rig(), build_effective()
    rig.evaluate.execute(effective)
    first = rig.record.execute(effective)
    second = rig.record.execute(effective)
    assert second.replayed and second.eligibility_id == first.eligibility_id
    assert len(rig.store.eligibility) == 1


def test_a_newer_run_supersedes_the_record_until_it_is_recorded() -> None:
    rig, effective = Rig(), build_effective()
    rig.evaluate.execute(effective)
    rig.record.execute(effective)
    rig.clock.advance(timedelta(seconds=10))
    rig.evaluate.execute(effective)
    assert rig.check.execute(effective).failures == (ELIGIBILITY_SUPERSEDED,)
    assert rig.record.execute(effective).decision.eligible
    assert rig.check.execute(effective).eligible
    assert len(rig.store.eligibility) == 2  # history is appended, never rewritten


def test_a_later_failing_run_withdraws_eligibility() -> None:
    rig, effective = Rig(), build_effective()
    rig.evaluate.execute(effective)
    rig.record.execute(effective)
    rig.clock.advance(timedelta(seconds=10))
    rig.set_target(failing_target())
    rig.evaluate.execute(effective)
    decision = rig.check.execute(effective)
    assert not decision.eligible and ELIGIBILITY_RUN_FAILED in decision.failures


def test_a_recent_failure_blocks_even_when_a_later_run_passes() -> None:
    """Concurrent or flaky runs on one configuration: any failure in the window blocks."""
    rig, effective = Rig(failing_target()), build_effective()
    rig.evaluate.execute(effective)
    rig.clock.advance(timedelta(seconds=10))
    rig.set_target(PerfectTarget())
    rig.evaluate.execute(effective)
    decision = rig.record.execute(effective).decision
    assert decision.failures == (ELIGIBILITY_RECENT_FAILURE,)
    rig.clock.advance(timedelta(seconds=MAX_AGE))  # the old failure ages out of the window
    rig.evaluate.execute(effective)
    assert rig.record.execute(effective).decision.eligible


def test_a_corrupted_stored_run_is_never_trusted() -> None:
    rig, effective = Rig(), build_effective()
    rig.evaluate.execute(effective)
    rig.record.execute(effective)
    rig.store.tamper(0, record_text=rig.store.runs[0].record_text.replace("true", "false", 1))
    assert rig.check.execute(effective).failures == (ELIGIBILITY_RECORD_CORRUPT,)


def test_a_record_naming_a_different_hash_is_superseded_not_trusted() -> None:
    rig, effective = Rig(), build_effective()
    rig.evaluate.execute(effective)
    rig.record.execute(effective)
    rig.store.eligibility[0] = replace(rig.store.eligibility[0], run_sha256="0" * 64)
    assert ELIGIBILITY_SUPERSEDED in rig.check.execute(effective).failures


def test_a_record_for_another_configuration_does_not_count() -> None:
    rig, effective = Rig(), build_effective()
    rig.evaluate.execute(effective)
    rig.record.execute(effective)
    rig.store.eligibility[0] = replace(rig.store.eligibility[0], config_id="rag-" + "0" * 64)
    # the snapshot finds no record for this configuration at all
    assert rig.check.execute(effective).failures == (ELIGIBILITY_NOT_RECORDED,)


def test_a_bare_id_or_untyped_configuration_is_refused_everywhere() -> None:
    rig = Rig()
    for use_case in (rig.evaluate, rig.record, rig.check):
        for bad in ("rag-" + "a" * 64, build_effective().configuration(), None):
            with pytest.raises(ValidationError):
                use_case.execute(bad)  # type: ignore[arg-type]


def test_an_unstorable_run_is_an_error_and_returns_no_result() -> None:
    rig, effective = Rig(), build_effective()

    class Down(InMemoryEvaluationStore):
        def append_run(self, record):  # type: ignore[no-untyped-def]
            raise ConflictError("evaluation_run_exists")

    rig.store = Down()
    rig.set_target(PerfectTarget())
    with pytest.raises(ConflictError):
        rig.evaluate.execute(effective)


def test_the_evaluate_use_case_refuses_criteria_for_another_dataset() -> None:
    other = release_criteria(MAX_AGE)
    with pytest.raises(ValidationError):
        EvaluateConfiguration(
            RunEvaluation(PerfectTarget(), default_metrics(), default_limits()),
            RELEASE_DATASET,
            replace(other, dataset_fingerprint="d" * 64),
            InMemoryEvaluationStore(),
            FakeClock(START),
            SequentialIdGenerator(),
        )


def test_a_config_mismatch_is_reported_for_a_forged_stored_id() -> None:
    from lda.modules.evaluation.api import verify_run

    rig, effective = Rig(), build_effective()
    rig.evaluate.execute(effective)
    stored = rig.store.runs[0]
    _, failures = verify_run(
        stored.record_text,
        stored.record_sha256,
        CRITERIA,
        "rag-" + "9" * 64,
        int(START.timestamp()),
    )
    assert failures == [ELIGIBILITY_CONFIG_MISMATCH]
