"""RunWatchdog: tenant-bound, idempotent, fail-closed recovery (ADR-010 §11)."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.ingestion.application.dto.runs import WatchdogCommand, WatchdogResult
from lda.modules.ingestion.application.ports import StageRetryable
from lda.modules.ingestion.domain.events import IngestionCompleted
from lda.modules.ingestion.domain.run import RunStatus, run_id_for
from lda.modules.ingestion.testing import (
    InMemoryIngestionStore,
    InMemoryIngestionUnitOfWorkFactory,
    ScriptedStage,
)
from lda.modules.ingestion.tests.support import (
    DELAY,
    MAX_REDELIVERIES,
    SYSTEM_A,
    SYSTEM_B,
    TENANT_A,
    V1,
    Rig,
    build_rig,
    command,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.ids import JobId

RUN_A = run_id_for(TENANT_A, V1, 1)


def wd(token: int = 1) -> WatchdogCommand:
    return WatchdogCommand(RUN_A, JobId(UUID(int=0xFD)), token)


def crash_leaving_a_running_run(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> Rig:
    rig = build_rig(memory_factory, clock, ids)
    memory_store.fail_commit_on = {1}
    with pytest.raises(InfrastructureError):
        rig.execute.execute(SYSTEM_A, command())
    memory_store.fail_commit_on = set()
    return rig


# ---- a running run ----------------------------------------------------------------------------


def test_a_run_with_recent_progress_is_left_alone_and_rechecked_when_it_could_stall(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = crash_leaving_a_running_run(memory_factory, memory_store, clock, ids)
    clock.advance(timedelta(seconds=600))
    result = rig.watchdog.execute(SYSTEM_A, wd())
    assert result == WatchdogResult(done=False, next_check_seconds=1200)  # 1800 - 600
    assert memory_store.runs[(TENANT_A, RUN_A)].status is RunStatus.RUNNING


def test_a_fractional_remaining_wait_rounds_up(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = crash_leaving_a_running_run(memory_factory, memory_store, clock, ids)
    clock.advance(DELAY - timedelta(milliseconds=1500))
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(False, 2)  # 1.5 s -> 2, not 1


def test_the_remaining_wait_rounds_up_and_is_at_least_one_second(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = crash_leaving_a_running_run(memory_factory, memory_store, clock, ids)
    clock.advance(DELAY - timedelta(milliseconds=500))
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(False, 1)
    clock.advance(timedelta(milliseconds=500))  # idle is now exactly the delay -> not < delay
    assert rig.watchdog.execute(SYSTEM_A, wd()).done is True


def test_a_stalled_run_is_dead_lettered_not_replayed_and_publishes_nothing(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = crash_leaving_a_running_run(memory_factory, memory_store, clock, ids)
    jobs, events = len(memory_store.committed_jobs), len(memory_store.committed_events)
    clock.advance(DELAY)
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(done=True)
    run = memory_store.runs[(TENANT_A, RUN_A)]
    assert (run.status, run.error_code) == (RunStatus.DEAD_LETTERED, "watchdog_stalled")
    assert len(memory_store.committed_jobs) == jobs  # no hidden re-enqueue
    assert len(memory_store.committed_events) == events  # the version stays pending
    # idempotent: a second firing changes nothing
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(done=True)
    assert memory_store.runs[(TENANT_A, RUN_A)].status is RunStatus.DEAD_LETTERED


def test_progress_resets_the_stall_clock(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    flaky = ScriptedStage("fake_one", [StageRetryable("busy")])
    rig = build_rig(memory_factory, clock, ids, stages=[flaky])
    rig.execute.execute(SYSTEM_A, command(attempt=1))  # retry recorded at t0
    clock.advance(timedelta(seconds=1700))
    rig.execute.execute(SYSTEM_A, command(attempt=2))  # completes at t0+1700 -> outcome unacked
    clock.advance(
        timedelta(seconds=200)
    )  # 1900 after creation, but only 200 after the last progress
    result = rig.watchdog.execute(SYSTEM_A, wd())
    assert result == WatchdogResult(done=False, next_check_seconds=1600)


def test_a_stalled_run_whose_watchdog_lost_its_lease_changes_nothing(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = crash_leaving_a_running_run(memory_factory, memory_store, clock, ids)
    clock.advance(DELAY)
    memory_store.fence_rejects = lambda j, t: True
    with pytest.raises(ConflictError, match="job_lease_lost"):
        rig.watchdog.execute(SYSTEM_A, wd())
    assert memory_store.runs[(TENANT_A, RUN_A)].status is RunStatus.RUNNING


# ---- outcomes Documents has not acknowledged ---------------------------------------------------


def completed(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> Rig:
    rig = build_rig(memory_factory, clock, ids)
    rig.execute.execute(SYSTEM_A, command())
    return rig


def test_an_acknowledged_outcome_is_done(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    from lda.modules.ingestion.application.dto.runs import RunReference

    rig = completed(memory_factory, clock, ids)
    rig.acknowledge.execute(SYSTEM_A, RunReference(RUN_A))
    clock.advance(DELAY * 10)
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(done=True)
    assert len(memory_factory.store.committed_events) == 1  # nothing re-published


def test_an_unacknowledged_outcome_is_republished_with_a_new_event_id_then_rechecked(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = completed(memory_factory, clock, ids)
    clock.advance(DELAY - timedelta(seconds=1))
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(False, 1)  # not yet
    assert len(memory_store.committed_events) == 1
    clock.advance(timedelta(seconds=1))
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(False, 1800)
    first, second = (e for e, _ in memory_store.committed_events)
    assert isinstance(second, IngestionCompleted) and second.run_id == first.run_id  # type: ignore[attr-defined]
    assert second.event_id != first.event_id  # a NEW event: the platform's dedupe must not eat it
    assert memory_store.runs[(TENANT_A, RUN_A)].redeliveries == 1


def test_redelivery_is_bounded(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = completed(memory_factory, clock, ids)
    for _ in range(MAX_REDELIVERIES):
        clock.advance(DELAY)
        assert rig.watchdog.execute(SYSTEM_A, wd()).done is False
    clock.advance(DELAY)
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(done=True)
    assert len(memory_store.committed_events) == 1 + MAX_REDELIVERIES  # visible in metrics instead
    assert memory_store.runs[(TENANT_A, RUN_A)].redeliveries == MAX_REDELIVERIES


def test_a_lost_lease_prevents_a_republish(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = completed(memory_factory, clock, ids)
    clock.advance(DELAY)
    memory_store.fail_commit_on = {len(memory_factory.begun)}
    with pytest.raises(InfrastructureError):
        rig.watchdog.execute(SYSTEM_A, wd())
    assert len(memory_store.committed_events) == 1
    assert memory_store.runs[(TENANT_A, RUN_A)].redeliveries == 0  # event and counter are atomic


# ---- nothing to do -----------------------------------------------------------------------------


def test_an_unknown_run_is_done(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = build_rig(memory_factory, clock, ids)  # e.g. the document was purged
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(done=True)


@pytest.mark.parametrize("final", ["dead_lettered", "cancelled"])
def test_a_dead_lettered_or_cancelled_run_is_done_and_stays_as_it_is(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    final: str,
) -> None:
    from lda.modules.ingestion.application.dto.runs import DocumentReferenceCommand

    rig = crash_leaving_a_running_run(memory_factory, memory_store, clock, ids)
    if final == "cancelled":
        rig.cancel.execute(SYSTEM_A, DocumentReferenceCommand(command().document))
    else:
        clock.advance(DELAY)
        rig.watchdog.execute(SYSTEM_A, wd())
    clock.advance(DELAY * 5)
    jobs = len(memory_store.committed_jobs)
    assert rig.watchdog.execute(SYSTEM_A, wd()) == WatchdogResult(done=True)
    assert memory_store.runs[(TENANT_A, RUN_A)].status.value == final
    assert len(memory_store.committed_jobs) == jobs and memory_store.committed_events == []


def test_a_watchdog_never_sees_another_tenants_run(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = crash_leaving_a_running_run(memory_factory, memory_store, clock, ids)
    clock.advance(DELAY * 3)
    other_tenants_watchdog = WatchdogCommand(RUN_A, JobId(UUID(int=1)), 1)
    assert rig.watchdog.execute(SYSTEM_B, other_tenants_watchdog) == WatchdogResult(done=True)
    assert memory_store.runs[(TENANT_A, RUN_A)].status is RunStatus.RUNNING  # untouched
