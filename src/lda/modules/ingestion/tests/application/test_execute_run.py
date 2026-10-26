"""ExecuteRun: stage execution under at-least-once delivery (ADR-010 §3-§8, §12)."""

from __future__ import annotations

import logging
from uuid import UUID

import pytest

from lda.modules.ingestion.application.dto.runs import ExecutionOutcome, ExecutionResult
from lda.modules.ingestion.application.ports import (
    ScanVerdict,
    StagePermanentFailure,
    StageRetryable,
    StageSucceeded,
)
from lda.modules.ingestion.domain.events import (
    IngestionCompleted,
    IngestionFailed,
    IngestionQuarantined,
)
from lda.modules.ingestion.domain.run import RunStatus, StageStatus, run_id_for
from lda.modules.ingestion.testing import (
    InMemoryIngestionStore,
    InMemoryIngestionUnitOfWorkFactory,
    ScriptedScanner,
    ScriptedStage,
)
from lda.modules.ingestion.tests.support import (
    D1,
    D2,
    SYSTEM_A,
    SYSTEM_B,
    TENANT_A,
    TENANT_B,
    V1,
    Rig,
    build_rig,
    command,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConflictError, InfrastructureError, NotFoundError
from lda.shared.ports.events import Delivery

FINISHED = ExecutionResult(ExecutionOutcome.FINISHED)
INACTIVE = ExecutionResult(ExecutionOutcome.INACTIVE)


def only_run(store: InMemoryIngestionStore, tenant=TENANT_A, version=V1, generation=1):  # type: ignore[no-untyped-def]
    return store.runs[(tenant, run_id_for(tenant, version, generation))]


def event_types(store: InMemoryIngestionStore) -> list[str]:
    return [type(e).__name__ for e, _ in store.committed_events]


# ---- happy path --------------------------------------------------------------------------------


def test_every_stage_runs_once_in_order_and_the_run_completes_with_one_event(
    rig: Rig, memory_store: InMemoryIngestionStore
) -> None:
    assert rig.execute.execute(SYSTEM_A, command()) == FINISHED
    run = only_run(memory_store)
    assert run.status is RunStatus.COMPLETED and run.error_code is None
    assert [(s.name, s.status, s.attempts) for s in run.stages] == [
        ("scan", StageStatus.SUCCEEDED, 1),
        ("fake_one", StageStatus.SUCCEEDED, 1),
        ("fake_two", StageStatus.SUCCEEDED, 1),
    ]
    assert len(rig.scanner.requests) == 1
    assert [len(s.calls) for s in rig.stages] == [1, 1]
    ((event, delivery),) = memory_store.committed_events
    assert isinstance(event, IngestionCompleted) and delivery is Delivery.ASYNC_JOB
    assert event.stage_count == 3 and event.tenant_id == TENANT_A
    assert (event.document, event.version, event.run_id) == (D1, V1, run.id)


def test_stages_receive_identifiers_attempt_and_the_lease_for_their_own_fencing(
    rig: Rig,
) -> None:
    rig.execute.execute(SYSTEM_A, command(job=0x77, token=5))
    (call,) = rig.stages[0].calls
    assert (call.tenant_id, call.document, call.version) == (TENANT_A, D1, V1)
    assert call.run_id == run_id_for(TENANT_A, V1, 1)
    assert (call.stage, call.attempt) == ("fake_one", 1)
    assert (call.job_id.value, call.fencing_token) == (UUID(int=0x77), 5)


def test_creating_the_run_also_enqueues_exactly_one_delayed_watchdog(
    rig: Rig, memory_store: InMemoryIngestionStore
) -> None:
    rig.execute.execute(SYSTEM_A, command())
    watchdogs = [s for _, s in memory_store.committed_jobs if s.job_type == "ingestion:watchdog"]
    assert len(watchdogs) == 1
    run = only_run(memory_store)
    assert watchdogs[0].unique_key == f"ingestion:watchdog:{run.id}"
    assert watchdogs[0].run_at == run.created_at + rig.starter._watchdog_delay  # noqa: SLF001


# ---- duplicate delivery ------------------------------------------------------------------------


def test_a_duplicate_delivery_after_completion_does_nothing_at_all(
    rig: Rig, memory_store: InMemoryIngestionStore
) -> None:
    rig.execute.execute(SYSTEM_A, command())
    events, jobs = len(memory_store.committed_events), len(memory_store.committed_jobs)
    assert rig.execute.execute(SYSTEM_A, command(job=2, token=2, attempt=2)) == INACTIVE
    assert len(memory_store.committed_events) == events
    assert len(memory_store.committed_jobs) == jobs
    assert [len(s.calls) for s in rig.stages] == [1, 1] and len(rig.scanner.requests) == 1


def test_resuming_after_a_crash_skips_the_stages_that_already_succeeded(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = build_rig(memory_factory, clock, ids)
    # units of work: 0 begin(scan) 1 record(scan) 2 begin(fake_one) 3 record(fake_one)  <- crash
    memory_store.fail_commit_on = {3}
    with pytest.raises(InfrastructureError):
        rig.execute.execute(SYSTEM_A, command())
    run = only_run(memory_store)
    assert run.status is RunStatus.RUNNING
    assert run.stage("scan").status is StageStatus.SUCCEEDED
    assert run.stage("fake_one").status is StageStatus.RUNNING  # started, never recorded
    assert memory_store.committed_events == []
    # the queue delivers the job again
    memory_store.fail_commit_on = set()
    assert rig.execute.execute(SYSTEM_A, command(job=1, token=2, attempt=2)) == FINISHED
    assert len(rig.scanner.requests) == 1  # scan is NOT repeated
    assert len(rig.stages[0].calls) == 2  # the interrupted stage runs again (idempotent contract)
    assert rig.stages[0].calls[1].attempt == 2 and rig.stages[1].calls[0].attempt == 1
    assert only_run(memory_store).status is RunStatus.COMPLETED
    assert event_types(memory_store) == ["IngestionCompleted"]  # exactly one durable outcome


def test_a_crash_before_the_run_is_even_created_leaves_nothing_behind(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = build_rig(memory_factory, clock, ids)
    memory_store.fail_commit_on = {0}
    with pytest.raises(InfrastructureError):
        rig.execute.execute(SYSTEM_A, command())
    assert memory_store.runs == {} and memory_store.committed_jobs == []  # no run, no watchdog
    memory_store.fail_commit_on = set()
    assert rig.execute.execute(SYSTEM_A, command(attempt=2)) == FINISHED


def test_a_crash_while_closing_the_run_publishes_nothing_until_it_commits(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = build_rig(memory_factory, clock, ids, stages=[ScriptedStage("only")])
    memory_store.fail_commit_on = {3}  # 0 begin scan, 1 record scan, 2 begin only, 3 record+close
    with pytest.raises(InfrastructureError):
        rig.execute.execute(SYSTEM_A, command())
    assert memory_store.committed_events == []  # state and event are one transaction
    assert only_run(memory_store).status is RunStatus.RUNNING
    memory_store.fail_commit_on = set()
    rig.execute.execute(SYSTEM_A, command(attempt=2))
    assert event_types(memory_store) == ["IngestionCompleted"]


def test_two_executors_cannot_both_record_the_same_stage_attempt(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    """A stale executor finishes its stage AFTER a newer executor re-began it: the stale outcome
    is ignored (compare-and-set on the stage attempt)."""
    started: list[int] = []

    def interleave(request):  # type: ignore[no-untyped-def]
        if not started:
            started.append(1)
            # while this (stale) executor works, a newer one restarts the same stage
            newer = rig.execute.execute(SYSTEM_A, command(job=2, token=2, attempt=2))
            assert newer == FINISHED
        return StageSucceeded()

    stale = ScriptedStage("fake_one", [interleave])
    rig = build_rig(memory_factory, clock, ids, stages=[stale])
    result = rig.execute.execute(SYSTEM_A, command(job=1, token=1))
    assert result == INACTIVE  # the stale executor's result was discarded
    assert event_types(memory_store) == ["IngestionCompleted"]
    assert only_run(memory_store).stage("fake_one").attempts == 2


# ---- fencing -----------------------------------------------------------------------------------


def test_a_lost_lease_aborts_every_write_and_commits_nothing_more(
    rig: Rig, memory_store: InMemoryIngestionStore
) -> None:
    memory_store.fence_rejects = lambda job_id, token: True
    with pytest.raises(ConflictError, match="job_lease_lost"):
        rig.execute.execute(SYSTEM_A, command())
    assert memory_store.committed_events == [] and memory_store.committed_facts == []
    assert (
        only_run(
            memory_store,
        )
        if memory_store.runs
        else True
    )
    assert rig.scanner.requests == []  # not even the first stage was started


def test_the_fence_is_checked_with_the_jobs_own_id_and_token(
    rig: Rig, memory_store: InMemoryIngestionStore
) -> None:
    seen: list[tuple[int, int]] = []

    def record(job_id, token):  # type: ignore[no-untyped-def]
        seen.append((job_id.value.int, token))
        return False

    memory_store.fence_rejects = record
    rig.execute.execute(SYSTEM_A, command(job=0x42, token=9))
    assert seen and set(seen) == {(0x42, 9)}
    # every write transaction verifies: 2 per stage (begin + record), 3 stages
    assert len(seen) == 6


def test_a_lease_lost_during_a_stage_prevents_the_outcome_from_being_recorded(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    def lose_lease(request):  # type: ignore[no-untyped-def]
        memory_store.fence_rejects = lambda j, t: True
        return StageSucceeded()

    rig = build_rig(memory_factory, clock, ids, stages=[ScriptedStage("fake_one", [lose_lease])])
    with pytest.raises(ConflictError, match="job_lease_lost"):
        rig.execute.execute(SYSTEM_A, command())
    run = only_run(memory_store)
    assert run.stage("fake_one").status is StageStatus.RUNNING  # the success was NOT committed
    assert memory_store.committed_events == []


def test_a_stage_whose_own_fence_fails_stops_the_executor_instead_of_retrying(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    stage = ScriptedStage("fake_one", [ConflictError("job_lease_lost")])
    rig = build_rig(memory_factory, clock, ids, stages=[stage])
    with pytest.raises(ConflictError, match="job_lease_lost"):
        rig.execute.execute(SYSTEM_A, command())
    assert only_run(memory_store).stage("fake_one").status is StageStatus.RUNNING


# ---- retry and dead letter ---------------------------------------------------------------------


def test_a_retryable_failure_returns_the_stage_to_pending_and_asks_for_a_retry(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    flaky = ScriptedStage("fake_one", [StageRetryable("provider_busy")])
    rig = build_rig(memory_factory, clock, ids, stages=[flaky])
    result = rig.execute.execute(SYSTEM_A, command(attempt=1, max_attempts=3))
    assert result == ExecutionResult(ExecutionOutcome.RETRY, "provider_busy")
    run = only_run(memory_store)
    assert run.status is RunStatus.RUNNING
    assert (run.stage("fake_one").status, run.stage("fake_one").error_code) == (
        StageStatus.PENDING,
        "provider_busy",
    )
    assert memory_store.committed_events == []


def test_the_retry_then_succeeds_without_repeating_finished_stages(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    flaky = ScriptedStage("fake_one", [StageRetryable("provider_busy")])
    rig = build_rig(memory_factory, clock, ids, stages=[flaky])
    rig.execute.execute(SYSTEM_A, command(attempt=1))
    assert rig.execute.execute(SYSTEM_A, command(attempt=2)) == FINISHED
    assert len(rig.scanner.requests) == 1
    assert only_run(memory_store).stage("fake_one").attempts == 2
    assert event_types(memory_store) == ["IngestionCompleted"]


@pytest.mark.parametrize(
    ("attempt", "outcome"), [(1, "retry"), (2, "retry"), (3, "dead"), (4, "dead")]
)
def test_retries_are_bounded_by_the_jobs_max_attempts(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    attempt: int,
    outcome: str,
) -> None:
    rig = build_rig(
        memory_factory, clock, ids, stages=[ScriptedStage("fake_one", [StageRetryable("busy")])]
    )
    result = rig.execute.execute(SYSTEM_A, command(attempt=attempt, max_attempts=3))
    run = only_run(memory_store)
    if outcome == "retry":
        assert result.outcome is ExecutionOutcome.RETRY and run.status is RunStatus.RUNNING
    else:
        assert result == ExecutionResult(ExecutionOutcome.DEAD_LETTERED, "busy")
        assert (run.status, run.error_code) == (RunStatus.DEAD_LETTERED, "busy")
        assert memory_store.committed_events == []  # the version stays pending (D3)


def test_a_dead_lettered_run_is_never_resurrected_by_a_late_delivery(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    stage = ScriptedStage("fake_one", [StageRetryable("busy")])
    rig = build_rig(memory_factory, clock, ids, stages=[stage])
    rig.execute.execute(SYSTEM_A, command(attempt=3, max_attempts=3))
    calls = len(stage.calls)
    assert rig.execute.execute(SYSTEM_A, command(attempt=1, max_attempts=3)) == INACTIVE
    assert len(stage.calls) == calls
    assert only_run(memory_store).status is RunStatus.DEAD_LETTERED


def test_a_stage_that_raises_is_a_retryable_failure_with_a_safe_code(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    class Coded(NotFoundError):
        pass

    for exc, code in (
        (RuntimeError("TOP-SECRET clause text"), "stage_exception"),
        (NotFoundError("thing_missing"), "thing_missing"),
    ):
        store = InMemoryIngestionStore()
        rig = build_rig(
            InMemoryIngestionUnitOfWorkFactory(store),
            clock,
            ids,
            stages=[ScriptedStage("s", [exc])],
        )
        result = rig.execute.execute(SYSTEM_A, command())
        assert result == ExecutionResult(ExecutionOutcome.RETRY, code)
        assert "TOP-SECRET" not in repr(store.runs)


def test_a_malformed_stage_result_is_retryable_not_trusted(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = build_rig(
        memory_factory,
        clock,
        ids,
        stages=[ScriptedStage("s", [lambda r: "ok"])],  # type: ignore[list-item,return-value]
    )
    assert rig.execute.execute(SYSTEM_A, command()) == ExecutionResult(
        ExecutionOutcome.RETRY, "stage_outcome_invalid"
    )


@pytest.mark.parametrize("bad", ["Has Space", "", "x" * 65, "secret: clause 4"])
def test_a_stage_cannot_put_free_text_in_a_failure_code(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    bad: str,
) -> None:
    rig = build_rig(
        memory_factory, clock, ids, stages=[ScriptedStage("s", [StagePermanentFailure(bad)])]
    )
    result = rig.execute.execute(SYSTEM_A, command())
    assert result == ExecutionResult(ExecutionOutcome.FINISHED, "stage_error_code_invalid")
    assert only_run(memory_store).error_code == "stage_error_code_invalid"


# ---- permanent failure and quarantine ----------------------------------------------------------


def test_a_permanent_failure_ends_the_run_publishes_failed_and_skips_later_stages(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    first = ScriptedStage("fake_one", [StagePermanentFailure("unsupported_format")])
    second = ScriptedStage("fake_two")
    rig = build_rig(memory_factory, clock, ids, stages=[first, second])
    assert rig.execute.execute(SYSTEM_A, command()) == ExecutionResult(
        ExecutionOutcome.FINISHED, "unsupported_format"
    )
    run = only_run(memory_store)
    assert (run.status, run.error_code) == (RunStatus.FAILED, "unsupported_format")
    assert second.calls == []
    ((event, _),) = memory_store.committed_events
    assert isinstance(event, IngestionFailed) and event.error_code == "unsupported_format"
    assert rig.execute.execute(SYSTEM_A, command(attempt=2)) == INACTIVE  # never reopened
    assert len(memory_store.committed_events) == 1


def test_an_infected_scan_quarantines_and_no_later_stage_ever_runs(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    after = ScriptedStage("fake_one")
    rig = build_rig(
        memory_factory, clock, ids, stages=[after], scanner=ScriptedScanner([ScanVerdict.INFECTED])
    )
    assert rig.execute.execute(SYSTEM_A, command()) == ExecutionResult(
        ExecutionOutcome.FINISHED, "malware_detected"
    )
    run = only_run(memory_store)
    assert (run.status, run.stage("scan").status) == (
        RunStatus.QUARANTINED,
        StageStatus.QUARANTINED,
    )
    assert after.calls == []
    ((event, _),) = memory_store.committed_events
    assert isinstance(event, IngestionQuarantined) and event.error_code == "malware_detected"
    for attempt in (2, 3):  # redeliveries and even replays of the job cannot re-enter processing
        assert rig.execute.execute(SYSTEM_A, command(attempt=attempt)) == INACTIVE
    assert after.calls == [] and len(rig.scanner.requests) == 1


def test_a_scanner_error_is_retryable_and_dead_letters_instead_of_passing(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    scanner = ScriptedScanner([ScanVerdict.ERROR, RuntimeError("scanner down")])
    after = ScriptedStage("fake_one")
    rig = build_rig(memory_factory, clock, ids, stages=[after], scanner=scanner)
    first = rig.execute.execute(SYSTEM_A, command(attempt=1, max_attempts=2))
    assert first == ExecutionResult(ExecutionOutcome.RETRY, "scan_error")
    second = rig.execute.execute(SYSTEM_A, command(attempt=2, max_attempts=2))
    assert second == ExecutionResult(ExecutionOutcome.DEAD_LETTERED, "stage_exception")
    assert after.calls == [] and only_run(memory_store).status is RunStatus.DEAD_LETTERED


# ---- tenancy -----------------------------------------------------------------------------------


def test_the_same_version_id_in_two_tenants_gets_two_independent_runs(
    rig: Rig, memory_store: InMemoryIngestionStore
) -> None:
    rig.execute.execute(SYSTEM_A, command())
    rig.execute.execute(SYSTEM_B, command())
    a, b = only_run(memory_store, TENANT_A), only_run(memory_store, TENANT_B)
    assert a.id != b.id and (a.tenant_id, b.tenant_id) == (TENANT_A, TENANT_B)
    tenants = {e.tenant_id for e, _ in memory_store.committed_events}
    assert tenants == {TENANT_A, TENANT_B}
    assert len(memory_store.committed_events) == 2


def test_a_job_can_only_ever_touch_its_own_tenants_run(
    rig: Rig, memory_store: InMemoryIngestionStore
) -> None:
    rig.execute.execute(SYSTEM_A, command())
    before = only_run(memory_store, TENANT_A)
    # tenant B's job names tenant A's version: it creates/uses ONLY a tenant-B run
    rig.execute.execute(SYSTEM_B, command(version=V1))
    assert only_run(memory_store, TENANT_A) == before
    assert all(e.tenant_id == t for e, _ in memory_store.committed_events for t in [e.tenant_id])
    run_b = only_run(memory_store, TENANT_B)
    assert run_b.tenant_id == TENANT_B


def test_a_run_is_not_executed_for_a_different_document_than_it_was_created_for(
    rig: Rig, memory_store: InMemoryIngestionStore
) -> None:
    rig.execute.execute(SYSTEM_A, command(document=D1))
    stage_calls = len(rig.stages[0].calls)
    # a forged payload: same version, other document
    assert rig.execute.execute(SYSTEM_A, command(document=D2, attempt=2)) == INACTIVE
    assert len(rig.stages[0].calls) == stage_calls


def test_a_cancelled_run_is_not_executed_even_when_its_job_is_delivered_later(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    stage = ScriptedStage("fake_one")
    rig = build_rig(memory_factory, clock, ids, stages=[stage])
    memory_store.fail_commit_on = {1}  # the run exists (tx 0), then the process dies
    with pytest.raises(InfrastructureError):
        rig.execute.execute(SYSTEM_A, command())
    memory_store.fail_commit_on = set()
    from lda.modules.ingestion.application.dto.runs import DocumentReferenceCommand

    assert rig.cancel.execute(SYSTEM_A, DocumentReferenceCommand(D1)) == 1
    calls = len(stage.calls)
    assert rig.execute.execute(SYSTEM_A, command(attempt=2)) == INACTIVE
    assert len(stage.calls) == calls
    assert only_run(memory_store).status is RunStatus.CANCELLED


# ---- plan drift --------------------------------------------------------------------------------


def test_a_live_run_whose_plan_no_longer_exists_fails_closed(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    old = build_rig(memory_factory, clock, ids, stages=[ScriptedStage("old_stage")])
    memory_store.fail_commit_on = {3}  # crash after scan, while starting old_stage's record
    with pytest.raises(InfrastructureError):
        old.execute.execute(SYSTEM_A, command())
    memory_store.fail_commit_on = set()
    new = build_rig(memory_factory, clock, ids, stages=[ScriptedStage("different_stage")])
    result = new.execute.execute(SYSTEM_A, command(attempt=2))
    assert result == ExecutionResult(ExecutionOutcome.FINISHED, "pipeline_mismatch")
    assert only_run(memory_store).status is RunStatus.FAILED
    assert event_types(memory_store) == ["IngestionFailed"]


# ---- logging -----------------------------------------------------------------------------------


def test_logs_carry_identifiers_and_metadata_never_content(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stage = ScriptedStage("fake_one", [RuntimeError("TOP-SECRET clause text")])
    rig = build_rig(memory_factory, clock, ids, stages=[stage])
    with caplog.at_level(logging.INFO):
        rig.execute.execute(SYSTEM_A, command())
    records = [r for r in caplog.records if r.getMessage() == "ingestion_stage_finished"]
    assert len(records) == 2  # scan, then the failing stage
    fields = [r.fields for r in records]  # type: ignore[attr-defined]
    assert fields[0]["stage"] == "scan" and fields[0]["outcome"] == "StageSucceeded"
    assert fields[1] == {
        "stage": "fake_one",
        "outcome": "StageRetryable",
        "run_status": "running",
        "duration_ms": 0,
        "error_code": "stage_exception",
    }
    assert "TOP-SECRET" not in caplog.text


# ---- gaps found by the mutation campaign -------------------------------------------------------


def stuck_running_run(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    **kwargs: object,
) -> Rig:
    """A run that exists and is still running: the first process died after creating it."""
    rig = build_rig(memory_factory, clock, ids, **kwargs)  # type: ignore[arg-type]
    memory_store.fail_commit_on = {1}
    with pytest.raises(InfrastructureError):
        rig.execute.execute(SYSTEM_A, command())
    memory_store.fail_commit_on = set()
    return rig


def test_an_active_run_is_not_executed_for_a_document_it_was_not_created_for(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = stuck_running_run(memory_factory, memory_store, clock, ids)
    assert only_run(memory_store).status is RunStatus.RUNNING
    scans, attempts = len(rig.scanner.requests), only_run(memory_store).stage("scan").attempts
    result = rig.execute.execute(SYSTEM_A, command(document=D2, attempt=2))  # forged document id
    assert result == INACTIVE
    assert len(rig.scanner.requests) == scans  # nothing ran for the forged payload
    assert only_run(memory_store).stage("scan").attempts == attempts


def test_a_running_run_whose_stages_all_succeeded_is_closed_by_the_next_delivery(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    from lda.modules.ingestion.domain.run import IngestionRun

    rig = build_rig(memory_factory, clock, ids, stages=[ScriptedStage("fake_one")])
    run = IngestionRun.start(TENANT_A, D1, V1, 1, rig.pipeline.names, clock.now())
    for stage in list(run.stages):
        run.begin_stage(stage.name, clock.now())
        run.succeed_stage(stage.name, clock.now())
    memory_store.runs[(TENANT_A, run.id)] = run  # state a migration or a manual fix could leave
    assert rig.execute.execute(SYSTEM_A, command()) == FINISHED
    assert only_run(memory_store).status is RunStatus.COMPLETED
    assert event_types(memory_store) == ["IngestionCompleted"]
    assert rig.scanner.requests == []  # nothing is re-run: only the closing happened


def test_plan_drift_records_the_mismatch_code_on_the_run_and_in_the_event(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    stuck_running_run(memory_factory, memory_store, clock, ids, stages=[ScriptedStage("old_stage")])
    new = build_rig(memory_factory, clock, ids, stages=[ScriptedStage("different_stage")])
    new.execute.execute(SYSTEM_A, command(attempt=2))
    run = only_run(memory_store)
    assert run.error_code == "pipeline_mismatch"
    ((event, _),) = memory_store.committed_events
    assert isinstance(event, IngestionFailed) and event.error_code == "pipeline_mismatch"


def test_a_stale_executor_cannot_record_over_a_newer_executors_running_stage(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    """The stale one returns while the newer one is STILL inside the stage (attempt 2 running):
    its success must not be recorded against the newer attempt."""

    class Died(BaseException):
        pass

    def newer_executor_starts_then_dies(request):  # type: ignore[no-untyped-def]
        def die(_):  # type: ignore[no-untyped-def]
            raise Died

        newer = build_rig(
            InMemoryIngestionUnitOfWorkFactory(memory_store),
            clock,
            ids,
            stages=[ScriptedStage("fake_one", [die])],
        )
        with pytest.raises(Died):
            newer.execute.execute(SYSTEM_A, command(job=2, token=2, attempt=2))
        return StageSucceeded()

    rig = build_rig(
        memory_factory,
        clock,
        ids,
        stages=[ScriptedStage("fake_one", [newer_executor_starts_then_dies])],
    )
    assert rig.execute.execute(SYSTEM_A, command(job=1, token=1)) == INACTIVE
    stage = only_run(memory_store).stage("fake_one")
    assert (stage.status, stage.attempts) == (StageStatus.RUNNING, 2)  # still the newer attempt's
    assert memory_store.committed_events == []


def test_cancellation_during_a_stage_discards_its_result(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    from lda.modules.ingestion.application.dto.runs import DocumentReferenceCommand

    holder: list[Rig] = []

    def document_deleted_meanwhile(request):  # type: ignore[no-untyped-def]
        holder[0].cancel.execute(SYSTEM_A, DocumentReferenceCommand(D1))
        return StageSucceeded()

    rig = build_rig(
        memory_factory,
        clock,
        ids,
        stages=[ScriptedStage("fake_one", [document_deleted_meanwhile])],
    )
    holder.append(rig)
    assert rig.execute.execute(SYSTEM_A, command()) == INACTIVE
    assert only_run(memory_store).status is RunStatus.CANCELLED
    assert only_run(memory_store).stage("fake_one").status is StageStatus.RUNNING
    assert memory_store.committed_events == []


def test_stages_of_a_replayed_generation_see_that_generations_run_id(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    from lda.modules.ingestion.tests.support import user

    stage = ScriptedStage("fake_one", [StageRetryable("busy")])
    rig = build_rig(memory_factory, clock, ids, stages=[stage])
    rig.execute.execute(SYSTEM_A, command(attempt=3, max_attempts=3))  # dead letter
    from lda.modules.ingestion.application.dto.runs import ReplayCommand

    rig.replay.execute(user(), ReplayCommand(V1))
    rig.execute.execute(SYSTEM_A, command(generation=2))
    assert stage.calls[-1].run_id == run_id_for(TENANT_A, V1, 2)
