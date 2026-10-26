"""The run state machine (ADR-010 §3, §6). Expected values are written out, not imported."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from lda.modules.ingestion.domain.errors import InvalidRun, InvalidTransition, valid_error_code
from lda.modules.ingestion.domain.ids import DocumentRef, VersionRef
from lda.modules.ingestion.domain.run import (
    IngestionRun,
    RunStatus,
    StageStatus,
    run_id_for,
    valid_stage_name,
)
from lda.modules.ingestion.tests.support import D1, START, TENANT_A, TENANT_B, V1, V2

NOW = START + timedelta(seconds=5)


def new_run(
    stages: tuple[str, ...] = ("scan", "alpha", "beta"), generation: int = 1
) -> IngestionRun:
    return IngestionRun.start(TENANT_A, D1, V1, generation, stages, START)


def run_all_stages(run: IngestionRun, at: datetime = NOW) -> None:
    for stage in run.stages:
        run.begin_stage(stage.name, at)
        run.succeed_stage(stage.name, at)


# ---- identity ----------------------------------------------------------------------------------


def test_run_ids_are_deterministic_and_depend_on_tenant_version_and_generation() -> None:
    a = run_id_for(TENANT_A, V1, 1)
    assert a == run_id_for(TENANT_A, V1, 1)
    assert (
        len(
            {
                a,
                run_id_for(TENANT_B, V1, 1),
                run_id_for(TENANT_A, V2, 1),
                run_id_for(TENANT_A, V1, 2),
            }
        )
        == 4
    )
    expected = uuid.uuid5(
        UUID("5d3f3d54-8f0a-4b62-9a0c-6a3b1d1c7e11"), f"{TENANT_A.value}:{V1.value}:1"
    )
    assert a.value == expected  # stable across releases: persisted runs are addressed by it


def test_a_new_run_is_running_with_all_stages_pending_in_plan_order() -> None:
    run = new_run()
    assert run.status is RunStatus.RUNNING and run.finished_at is None
    assert [(s.name, s.position, s.status, s.attempts) for s in run.stages] == [
        ("scan", 0, StageStatus.PENDING, 0),
        ("alpha", 1, StageStatus.PENDING, 0),
        ("beta", 2, StageStatus.PENDING, 0),
    ]
    assert run.created_at == START and run.progress_at == START
    assert run.generation == 1 and run.redeliveries == 0 and run.acknowledged_at is None


@pytest.mark.parametrize(
    "stages",
    [(), ("a", "a"), ("Bad",), ("1x",), ("a-b",), ("",), ("a" * 65,)],
)
def test_a_plan_must_be_non_empty_unique_and_made_of_valid_names(stages: tuple[str, ...]) -> None:
    with pytest.raises(InvalidRun):
        IngestionRun.start(TENANT_A, D1, V1, 1, stages, START)


@pytest.mark.parametrize("generation", [0, -1])
def test_generation_starts_at_one(generation: int) -> None:
    with pytest.raises(InvalidRun, match="ingestion_generation_invalid"):
        new_run(generation=generation)


def test_stage_names_accept_exactly_the_documented_shape() -> None:
    assert valid_stage_name("a") == "a"
    assert valid_stage_name("a" + "b" * 63) == "a" + "b" * 63
    for bad in ("", "A", "9a", "a.b", "a" * 65, "a b"):
        with pytest.raises(InvalidRun):
            valid_stage_name(bad)


def test_error_codes_accept_exactly_the_documented_shape() -> None:
    for good in ("a", "scan_error", "a.b_c9", "x" * 64):
        assert valid_error_code(good) == good
    for bad in ("", "A", "9a", "a b", "a-b", "x" * 65, "secret text", "a\n"):
        with pytest.raises(InvalidRun):
            valid_error_code(bad)


# ---- stage transitions -------------------------------------------------------------------------


def test_stages_run_strictly_in_plan_order() -> None:
    run = new_run()
    with pytest.raises(InvalidTransition):
        run.begin_stage("alpha", NOW)  # scan has not succeeded
    run.begin_stage("scan", NOW)
    with pytest.raises(InvalidTransition):
        run.begin_stage("beta", NOW)
    assert run.next_stage() is run.stage("scan")


def test_beginning_a_stage_counts_the_attempt_and_stamps_the_time() -> None:
    run = new_run()
    run.begin_stage("scan", NOW)
    scan = run.stage("scan")
    assert (scan.status, scan.attempts, scan.started_at) == (StageStatus.RUNNING, 1, NOW)
    assert run.progress_at == NOW
    later = NOW + timedelta(seconds=3)
    run.begin_stage("scan", later)  # the previous executor died: the stage starts again
    assert (scan.status, scan.attempts, scan.started_at) == (StageStatus.RUNNING, 2, later)


def test_a_stage_cannot_succeed_unless_it_is_running() -> None:
    run = new_run()
    with pytest.raises(InvalidTransition):
        run.succeed_stage("scan", NOW)


def test_success_records_duration_in_milliseconds_and_clears_the_error() -> None:
    run = new_run()
    run.begin_stage("scan", NOW)
    run.retry_stage("scan", "scan_error", NOW)
    assert run.stage("scan").error_code == "scan_error"
    run.begin_stage("scan", NOW)
    run.succeed_stage("scan", NOW + timedelta(milliseconds=1500))
    scan = run.stage("scan")
    assert (scan.status, scan.duration_ms, scan.error_code) == (StageStatus.SUCCEEDED, 1500, None)
    assert scan.finished_at == NOW + timedelta(milliseconds=1500)


def test_a_retryable_failure_puts_the_stage_back_to_pending_and_keeps_the_run_running() -> None:
    run = new_run()
    run.begin_stage("scan", NOW)
    run.retry_stage("scan", "scan_error", NOW)
    scan = run.stage("scan")
    assert (scan.status, scan.attempts, scan.error_code) == (StageStatus.PENDING, 1, "scan_error")
    assert run.status is RunStatus.RUNNING and scan.finished_at is None


def test_a_permanent_failure_fails_stage_and_run() -> None:
    run = new_run()
    run.begin_stage("scan", NOW)
    run.succeed_stage("scan", NOW)
    run.begin_stage("alpha", NOW)
    run.fail_stage("alpha", "unsupported", NOW)
    assert run.stage("alpha").status is StageStatus.FAILED
    assert (run.status, run.error_code, run.finished_at) == (RunStatus.FAILED, "unsupported", NOW)
    assert run.next_stage() is None  # nothing runs any more
    assert run.stage("beta").status is StageStatus.PENDING


def test_quarantine_closes_the_run_and_nothing_after_it_runs() -> None:
    run = new_run()
    run.begin_stage("scan", NOW)
    run.quarantine_stage("scan", "malware_detected", NOW)
    assert run.status is RunStatus.QUARANTINED and run.error_code == "malware_detected"
    assert run.stage("scan").status is StageStatus.QUARANTINED
    with pytest.raises(InvalidTransition):
        run.begin_stage("alpha", NOW)
    with pytest.raises(InvalidTransition):
        run.complete(NOW)


@pytest.mark.parametrize("code", ["", "Has Space", "x" * 65])
def test_a_bad_failure_code_is_rejected_before_any_state_changes(code: str) -> None:
    run = new_run()
    run.begin_stage("scan", NOW)
    for action in (run.retry_stage, run.fail_stage, run.quarantine_stage):
        with pytest.raises(InvalidRun):
            action("scan", code, NOW)
    assert run.status is RunStatus.RUNNING and run.stage("scan").status is StageStatus.RUNNING


# ---- the completion condition ------------------------------------------------------------------


def test_complete_requires_every_stage_to_have_succeeded() -> None:
    run = new_run()
    with pytest.raises(InvalidTransition):
        run.complete(NOW)
    run.begin_stage("scan", NOW)
    run.succeed_stage("scan", NOW)
    run.begin_stage("alpha", NOW)
    run.succeed_stage("alpha", NOW)
    with pytest.raises(InvalidTransition):  # beta is still pending
        run.complete(NOW)
    run.begin_stage("beta", NOW)
    with pytest.raises(InvalidTransition):  # beta is running, not succeeded
        run.complete(NOW)
    run.succeed_stage("beta", NOW)
    run.complete(NOW)
    assert (run.status, run.finished_at, run.error_code) == (RunStatus.COMPLETED, NOW, None)


def test_a_completed_run_is_final() -> None:
    run = new_run(("scan",))
    run_all_stages(run)
    run.complete(NOW)
    forbidden_moves: list[Callable[[], object]] = [
        lambda: run.complete(NOW),
        lambda: run.dead_letter("x", NOW),
        lambda: run.cancel(NOW),
        lambda: run.begin_stage("scan", NOW),
    ]
    for forbidden in forbidden_moves:
        with pytest.raises(InvalidTransition):
            forbidden()
    assert run.status is RunStatus.COMPLETED


# ---- dead letter, cancel -----------------------------------------------------------------------


def test_only_a_running_run_can_be_dead_lettered_and_it_keeps_its_code() -> None:
    run = new_run()
    run.dead_letter("watchdog_stalled", NOW)
    assert (run.status, run.error_code, run.finished_at) == (
        RunStatus.DEAD_LETTERED,
        "watchdog_stalled",
        NOW,
    )
    with pytest.raises(InvalidTransition):
        run.dead_letter("again", NOW)
    assert run.next_stage() is None


def test_a_dead_lettered_run_cannot_progress_but_can_be_cancelled() -> None:
    run = new_run()
    run.dead_letter("x", NOW)
    with pytest.raises(InvalidTransition):
        run.begin_stage("scan", NOW)
    later = NOW + timedelta(seconds=9)
    run.cancel(later)
    assert (run.status, run.error_code, run.finished_at) == (RunStatus.CANCELLED, "x", later)


def test_cancel_works_from_running_and_dead_lettered_only() -> None:
    running = new_run()
    running.cancel(NOW)
    assert running.status is RunStatus.CANCELLED
    with pytest.raises(InvalidTransition):
        running.cancel(NOW)
    for finish in ("fail", "quarantine"):
        run = new_run()
        run.begin_stage("scan", NOW)
        if finish == "fail":
            run.fail_stage("scan", "bad", NOW)
        else:
            run.quarantine_stage("scan", "malware_detected", NOW)
        with pytest.raises(InvalidTransition):
            run.cancel(NOW)  # an outcome Documents may still need is never overwritten


# ---- outcome delivery --------------------------------------------------------------------------


def make_outcome_run(kind: str) -> IngestionRun:
    run = new_run(("scan",))
    run.begin_stage("scan", NOW)
    if kind == "completed":
        run.succeed_stage("scan", NOW)
        run.complete(NOW)
    elif kind == "failed":
        run.fail_stage("scan", "bad", NOW)
    else:
        run.quarantine_stage("scan", "malware_detected", NOW)
    return run


@pytest.mark.parametrize("kind", ["completed", "failed", "quarantined"])
def test_acknowledge_records_once_and_is_idempotent(kind: str) -> None:
    run = make_outcome_run(kind)
    assert run.has_outcome
    assert run.acknowledge(NOW) is True and run.acknowledged_at == NOW
    assert run.acknowledge(NOW + timedelta(seconds=9)) is False
    assert run.acknowledged_at == NOW  # the first acknowledgement stands


@pytest.mark.parametrize("build", ["running", "dead_lettered", "cancelled"])
def test_only_an_outcome_can_be_acknowledged_or_redelivered(build: str) -> None:
    run = new_run()
    if build == "dead_lettered":
        run.dead_letter("x", NOW)
    if build == "cancelled":
        run.cancel(NOW)
    assert not run.has_outcome
    with pytest.raises(InvalidTransition):
        run.acknowledge(NOW)
    with pytest.raises(InvalidTransition):
        run.record_redelivery(NOW)


def test_redelivery_counts_moves_progress_and_stops_after_acknowledgement() -> None:
    run = make_outcome_run("completed")
    later = NOW + timedelta(seconds=40)
    run.record_redelivery(later)
    assert (run.redeliveries, run.progress_at) == (1, later)
    run.record_redelivery(later)
    assert run.redeliveries == 2
    run.acknowledge(later)
    with pytest.raises(InvalidTransition):
        run.record_redelivery(later)


def test_queries() -> None:
    run = new_run(("scan", "alpha"))
    assert run.is_active and not run.has_outcome
    assert run.next_stage() is run.stage("scan")
    run.begin_stage("scan", NOW)
    run.succeed_stage("scan", NOW)
    assert run.next_stage() is run.stage("alpha")
    with pytest.raises(InvalidTransition):
        run.stage("missing")
    other = IngestionRun.start(
        TENANT_A, DocumentRef(UUID(int=1)), VersionRef(UUID(int=2)), 4, ("a",), START
    )
    assert other.generation == 4 and other.document.value == UUID(int=1)


# ---- progress timestamps and code validation (each transition moves the stall clock) -----------


def test_every_transition_moves_the_progress_clock_to_its_own_time() -> None:
    run = new_run(("scan", "alpha"))
    t = [NOW + timedelta(seconds=i) for i in range(1, 12)]
    run.begin_stage("scan", t[0])
    assert run.progress_at == t[0]
    run.retry_stage("scan", "busy", t[1])
    assert run.progress_at == t[1]  # not the begin time
    run.begin_stage("scan", t[2])
    run.succeed_stage("scan", t[3])
    assert run.progress_at == t[3]
    run.begin_stage("alpha", t[4])
    run.succeed_stage("alpha", t[5])
    run.complete(t[6])
    assert run.progress_at == t[6] and run.finished_at == t[6]


def test_dead_letter_and_cancel_stamp_progress_and_finish_time() -> None:
    dead = new_run()
    dead.dead_letter("stalled", NOW)
    assert (dead.progress_at, dead.finished_at) == (NOW, NOW)
    later = NOW + timedelta(seconds=7)
    dead.cancel(later)
    assert (dead.progress_at, dead.finished_at) == (later, later)
    cancelled = new_run()
    cancelled.cancel(NOW)
    assert (cancelled.progress_at, cancelled.finished_at) == (NOW, NOW)


def test_failure_and_quarantine_stamp_progress_and_finish_time() -> None:
    failed = new_run()
    failed.begin_stage("scan", NOW)
    at = NOW + timedelta(seconds=2)
    failed.fail_stage("scan", "bad", at)
    assert (failed.progress_at, failed.finished_at) == (at, at)
    held = new_run()
    held.begin_stage("scan", NOW)
    held.quarantine_stage("scan", "malware_detected", at)
    assert (held.progress_at, held.finished_at) == (at, at)


def test_dead_letter_rejects_a_bad_code_without_changing_state() -> None:
    run = new_run()
    for bad in ("", "Has Space", "x" * 65):
        with pytest.raises(InvalidRun):
            run.dead_letter(bad, NOW)
    assert run.status is RunStatus.RUNNING and run.finished_at is None and run.error_code is None
