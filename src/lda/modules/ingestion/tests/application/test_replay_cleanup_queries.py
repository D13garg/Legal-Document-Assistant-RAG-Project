"""Replay, acknowledge, cancel, purge and the operational queries (ADR-010 §9, §10, §14)."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.ingestion.application.dto.runs import (
    DocumentReferenceCommand,
    ReplayCommand,
    RunReference,
    VersionQuery,
)
from lda.modules.ingestion.application.ports import (
    ScanVerdict,
    StagePermanentFailure,
    StageRetryable,
)
from lda.modules.ingestion.domain.run import RunStatus, run_id_for
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
    V2,
    Rig,
    build_rig,
    command,
    user,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConflictError, NotFoundError
from lda.shared.ports.audit import AuditOutcome

REPLAY = ReplayCommand(V1)


def dead_lettered(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    **kwargs: object,
) -> Rig:
    stage = ScriptedStage("fake_one", [StageRetryable("busy")])
    rig = build_rig(memory_factory, clock, ids, stages=[stage], **kwargs)  # type: ignore[arg-type]
    rig.execute.execute(SYSTEM_A, command(attempt=3, max_attempts=3))
    assert memory_factory.store.runs[(TENANT_A, run_id_for(TENANT_A, V1, 1))].status is (
        RunStatus.DEAD_LETTERED
    )
    return rig


# ---- replay ------------------------------------------------------------------------------------


def test_replay_creates_the_next_generation_with_its_own_job_watchdog_and_audit_fact(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = dead_lettered(memory_factory, clock, ids)
    jobs_before = len(memory_store.committed_jobs)
    view = rig.replay.execute(user(), REPLAY)
    assert view.generation == 2 and view.run_id == run_id_for(TENANT_A, V1, 2)
    new = memory_store.runs[(TENANT_A, view.run_id)]
    assert new.status is RunStatus.RUNNING and [s.name for s in new.stages] == [
        "scan",
        "fake_one",
    ]
    old = memory_store.runs[(TENANT_A, run_id_for(TENANT_A, V1, 1))]
    assert old.status is RunStatus.DEAD_LETTERED  # the dead run stays as evidence
    added = [s for _, s in memory_store.committed_jobs[jobs_before:]]
    assert sorted(s.job_type for s in added) == ["ingestion:run", "ingestion:watchdog"]
    run_job = next(s for s in added if s.job_type == "ingestion:run")
    assert run_job.unique_key == f"ingestion:run:{V1.value}:2"
    assert dict(run_job.payload) == {
        "document_id": str(D1.value),
        "version_id": str(V1.value),
        "generation": 2,
    }
    ((fact,),) = [memory_store.committed_facts]
    assert fact.action == "ingestion.ingestion_replayed" and fact.outcome is AuditOutcome.SUCCESS
    assert (fact.tenant_id, fact.resource_type, fact.resource_id) == (
        TENANT_A,
        "ingestion_run",
        str(view.run_id),
    )
    assert dict(fact.attributes)["version_id"] == str(V1.value)
    assert dict(fact.attributes)["generation"] == "2"
    assert "TOP-SECRET" not in repr(fact)


def test_a_replayed_run_executes_with_a_fresh_budget_and_can_complete(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = dead_lettered(memory_factory, clock, ids)
    rig.replay.execute(user(), REPLAY)
    assert rig.execute.execute(SYSTEM_A, command(generation=2, attempt=1)).error_code is None
    assert memory_store.runs[(TENANT_A, run_id_for(TENANT_A, V1, 2))].status is RunStatus.COMPLETED
    assert [type(e).__name__ for e, _ in memory_store.committed_events] == ["IngestionCompleted"]


def test_a_second_replay_is_refused_while_the_first_is_in_flight(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = dead_lettered(memory_factory, clock, ids)
    rig.replay.execute(user(), REPLAY)
    with pytest.raises(ConflictError, match="ingestion_replay_not_allowed"):
        rig.replay.execute(user(), REPLAY)


def test_replaying_the_same_generation_twice_enqueues_one_job_even_if_the_run_check_passed(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    """Belt and braces: the unique key alone also refuses a duplicate replay request."""
    rig = dead_lettered(memory_factory, clock, ids)
    memory_store.unique_keys.add((TENANT_A, f"ingestion:run:{V1.value}:2"))
    with pytest.raises(ConflictError, match="ingestion_replay_already_requested"):
        rig.replay.execute(user(), REPLAY)
    assert (TENANT_A, run_id_for(TENANT_A, V1, 2)) not in memory_store.runs  # rolled back
    assert memory_store.committed_facts == []


def test_replay_of_an_unknown_version_is_not_found(rig: Rig) -> None:
    with pytest.raises(NotFoundError, match="ingestion_run_not_found"):
        rig.replay.execute(user(), REPLAY)


@pytest.mark.parametrize("kind", ["completed", "failed", "quarantined", "running", "cancelled"])
def test_replay_never_touches_a_run_that_is_not_dead_lettered(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    kind: str,
) -> None:
    stages = [ScriptedStage("fake_one", [StagePermanentFailure("bad")] if kind == "failed" else [])]
    scanner = ScriptedScanner([ScanVerdict.INFECTED] if kind == "quarantined" else [])
    rig = build_rig(memory_factory, clock, ids, stages=stages, scanner=scanner)
    if kind == "running":
        memory_store.fail_commit_on = {1}
        with pytest.raises(Exception):  # noqa: B017
            rig.execute.execute(SYSTEM_A, command())
        memory_store.fail_commit_on = set()
    else:
        rig.execute.execute(SYSTEM_A, command())
    if kind == "cancelled":
        rig.cancel.execute(SYSTEM_A, DocumentReferenceCommand(D1))
    runs, jobs, facts = (
        dict(memory_store.runs),
        len(memory_store.committed_jobs),
        len(memory_store.committed_facts),
    )
    with pytest.raises(ConflictError, match="ingestion_replay_not_allowed"):
        rig.replay.execute(user(), REPLAY)
    assert memory_store.runs.keys() == runs.keys() and len(memory_store.committed_jobs) == jobs
    assert len(memory_store.committed_facts) == facts


def test_replay_is_tenant_bound(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = dead_lettered(memory_factory, clock, ids)
    with pytest.raises(NotFoundError):
        rig.replay.execute(user(TENANT_B), REPLAY)  # tenant B cannot replay tenant A's run


# ---- acknowledge -------------------------------------------------------------------------------


def test_acknowledge_records_the_time_once_and_is_idempotent(
    rig: Rig, memory_store: InMemoryIngestionStore, clock: FakeClock
) -> None:
    rig.execute.execute(SYSTEM_A, command())
    run_id = run_id_for(TENANT_A, V1, 1)
    assert rig.acknowledge.execute(SYSTEM_A, RunReference(run_id)) is True
    stamp = memory_store.runs[(TENANT_A, run_id)].acknowledged_at
    assert stamp == clock.now()
    clock.advance(timedelta(seconds=30))
    assert rig.acknowledge.execute(SYSTEM_A, RunReference(run_id)) is False
    assert memory_store.runs[(TENANT_A, run_id)].acknowledged_at == stamp


def test_acknowledge_ignores_unknown_runs_runs_without_an_outcome_and_other_tenants(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    rig: Rig,
) -> None:
    assert rig.acknowledge.execute(SYSTEM_A, RunReference(run_id_for(TENANT_A, V1, 1))) is False
    dead = dead_lettered(memory_factory, clock, ids)
    run_id = run_id_for(TENANT_A, V1, 1)
    assert dead.acknowledge.execute(SYSTEM_A, RunReference(run_id)) is False  # not an outcome
    done = build_rig(memory_factory, clock, ids)
    done.execute.execute(SYSTEM_A, command(version=V2))
    v2_run = run_id_for(TENANT_A, V2, 1)
    assert done.acknowledge.execute(SYSTEM_B, RunReference(v2_run)) is False  # wrong tenant
    assert memory_store.runs[(TENANT_A, v2_run)].acknowledged_at is None


# ---- cancel and purge --------------------------------------------------------------------------


def test_cancel_stops_unfinished_runs_of_the_document_and_keeps_outcomes(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = build_rig(memory_factory, clock, ids)
    rig.execute.execute(SYSTEM_A, command(version=V1))  # completed
    memory_store.fail_commit_on = {len(memory_factory.begun) + 1}
    with pytest.raises(Exception):  # noqa: B017
        rig.execute.execute(SYSTEM_A, command(version=V2))  # running
    memory_store.fail_commit_on = set()
    rig.execute.execute(SYSTEM_A, command(version=VersionRef2, document=D2))  # other document
    assert rig.cancel.execute(SYSTEM_A, DocumentReferenceCommand(D1)) == 1
    status = {k[1]: r.status for k, r in memory_store.runs.items()}
    assert status[run_id_for(TENANT_A, V1, 1)] is RunStatus.COMPLETED
    assert status[run_id_for(TENANT_A, V2, 1)] is RunStatus.CANCELLED
    assert status[run_id_for(TENANT_A, VersionRef2, 1)] is RunStatus.COMPLETED  # other document
    assert rig.cancel.execute(SYSTEM_A, DocumentReferenceCommand(D1)) == 0  # idempotent


def test_cancel_also_covers_dead_lettered_runs_and_is_tenant_bound(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    memory_store: InMemoryIngestionStore,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = dead_lettered(memory_factory, clock, ids)
    assert rig.cancel.execute(SYSTEM_B, DocumentReferenceCommand(D1)) == 0
    assert memory_store.runs[(TENANT_A, run_id_for(TENANT_A, V1, 1))].status is (
        RunStatus.DEAD_LETTERED
    )
    assert rig.cancel.execute(SYSTEM_A, DocumentReferenceCommand(D1)) == 1
    assert memory_store.runs[(TENANT_A, run_id_for(TENANT_A, V1, 1))].status is RunStatus.CANCELLED


def test_purge_removes_only_the_documents_own_ingestion_state_in_its_tenant(
    rig: Rig, memory_store: InMemoryIngestionStore
) -> None:
    rig.execute.execute(SYSTEM_A, command(version=V1, document=D1))
    rig.execute.execute(SYSTEM_A, command(version=V2, document=D2))
    rig.execute.execute(SYSTEM_B, command(version=V1, document=D1))
    assert rig.purge.execute(SYSTEM_A, DocumentReferenceCommand(D1)) == 1
    left = {(t.value.int, r.document.value.int) for (t, _), r in memory_store.runs.items()}
    assert left == {(TENANT_A.value.int, D2.value.int), (TENANT_B.value.int, D1.value.int)}
    assert rig.purge.execute(SYSTEM_A, DocumentReferenceCommand(D1)) == 0  # idempotent
    assert memory_store.committed_facts == []  # and it writes no receipt or audit of its own


# ---- queries -----------------------------------------------------------------------------------


def test_status_returns_the_latest_generation_with_stage_details_and_no_content(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    rig = dead_lettered(memory_factory, clock, ids)
    first = rig.status.execute(user(), VersionQuery(V1))
    assert first is not None and (first.generation, first.status) == (1, "dead_lettered")
    assert first.error_code == "busy" and first.acknowledged is False
    assert [(s.name, s.status, s.attempts) for s in first.stages] == [
        ("scan", "succeeded", 1),
        ("fake_one", "pending", 1),
    ]
    rig.replay.execute(user(), REPLAY)
    latest = rig.status.execute(user(), VersionQuery(V1))
    assert latest is not None and (latest.generation, latest.status) == (2, "running")
    assert rig.status.execute(user(), VersionQuery(V2)) is None
    assert rig.status.execute(user(TENANT_B), VersionQuery(V1)) is None


def test_metrics_aggregate_runs_and_stages_for_one_tenant_only(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    stage = ScriptedStage("fake_one", [StageRetryable("busy")])
    rig = build_rig(memory_factory, clock, ids, stages=[stage])
    rig.execute.execute(SYSTEM_A, command(version=V1, attempt=1))  # retry
    clock.advance(timedelta(seconds=2))
    rig.execute.execute(SYSTEM_A, command(version=V1, attempt=2))  # completes
    rig.execute.execute(SYSTEM_A, command(version=V2, attempt=1, max_attempts=1))  # completes
    rig.execute.execute(SYSTEM_B, command(version=V1))  # other tenant
    m = rig.metrics.execute(user())
    assert dict(m.runs_by_status) == {"completed": 2}
    assert (m.dead_lettered_runs, m.unacknowledged_outcomes) == (0, 2)
    by_name = {s.stage: s for s in m.stages}
    assert set(by_name) == {"scan", "fake_one"}
    assert (
        by_name["fake_one"].runs,
        by_name["fake_one"].retries,
        by_name["fake_one"].failures,
    ) == (
        2,
        1,
        0,
    )
    assert by_name["scan"].runs == 2 and by_name["scan"].retries == 0
    assert by_name["fake_one"].maximum_duration_ms is not None
    other = rig.metrics.execute(user(TENANT_B))
    assert dict(other.runs_by_status) == {"completed": 1}


def test_metrics_count_dead_letters_failures_and_unacknowledged_outcomes(
    memory_factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    stage = ScriptedStage("fake_one", [StageRetryable("busy"), StagePermanentFailure("bad")])
    rig = build_rig(memory_factory, clock, ids, stages=[stage])
    rig.execute.execute(SYSTEM_A, command(version=V1, attempt=1, max_attempts=1))  # dead letter
    rig.execute.execute(SYSTEM_A, command(version=V2))  # permanent failure
    m = rig.metrics.execute(user())
    assert dict(m.runs_by_status) == {"dead_lettered": 1, "failed": 1}
    assert (m.dead_lettered_runs, m.unacknowledged_outcomes) == (1, 1)
    fake = next(s for s in m.stages if s.stage == "fake_one")
    assert fake.failures == 1 and fake.average_duration_ms is None  # no succeeded stage run
    rig.acknowledge.execute(SYSTEM_A, RunReference(run_id_for(TENANT_A, V2, 1)))
    assert rig.metrics.execute(user()).unacknowledged_outcomes == 0


VersionRef2 = type(V1)(UUID(int=0x99))
