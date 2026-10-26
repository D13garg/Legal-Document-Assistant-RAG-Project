"""One behavioural contract, run against the in-memory and the SQL unit of work."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.ingestion.application.ports import IngestionUnitOfWorkFactory
from lda.modules.ingestion.domain.ids import DocumentRef, VersionRef
from lda.modules.ingestion.domain.run import IngestionRun, RunStatus, StageStatus, run_id_for
from lda.modules.ingestion.tests.support import (
    D1,
    D2,
    START,
    SYSTEM_A,
    SYSTEM_B,
    TENANT_A,
    TENANT_B,
    V1,
    V2,
)
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId

NOW = START + timedelta(seconds=5)


@pytest.fixture(params=["memory", "sql"])
def factory(request: pytest.FixtureRequest) -> IngestionUnitOfWorkFactory:
    name = "memory_factory" if request.param == "memory" else "sql_factory"
    value: IngestionUnitOfWorkFactory = request.getfixturevalue(name)
    return value


def make(
    tenant: TenantId = TENANT_A,
    document: DocumentRef = D1,
    version: VersionRef = V1,
    generation: int = 1,
) -> IngestionRun:
    return IngestionRun.start(tenant, document, version, generation, ("scan", "alpha"), START)


def ctx(tenant: TenantId):  # type: ignore[no-untyped-def]
    return SYSTEM_A if tenant == TENANT_A else SYSTEM_B


def put(factory: IngestionUnitOfWorkFactory, run: IngestionRun) -> None:
    with factory.begin(ctx(run.tenant_id)) as uow:
        uow.runs.add(run)
        uow.commit()


def fetch(
    factory: IngestionUnitOfWorkFactory, tenant: TenantId, run: IngestionRun
) -> IngestionRun | None:
    with factory.begin(ctx(tenant)) as uow:
        return uow.runs.get(run.id)


def test_a_run_round_trips_with_its_stages_in_order(factory: IngestionUnitOfWorkFactory) -> None:
    run = make()
    put(factory, run)
    loaded = fetch(factory, TENANT_A, run)
    assert loaded == run
    assert [s.name for s in loaded.stages] == ["scan", "alpha"]


def test_nothing_is_visible_before_commit_and_rollback_discards(
    factory: IngestionUnitOfWorkFactory,
) -> None:
    run = make()
    with factory.begin(SYSTEM_A) as uow:
        uow.runs.add(run)
        uow.rollback()
    assert fetch(factory, TENANT_A, run) is None
    with factory.begin(SYSTEM_A) as uow:  # leaving the block without commit also discards
        uow.runs.add(run)
    assert fetch(factory, TENANT_A, run) is None


def test_lookups_by_version_generation_latest_and_document(
    factory: IngestionUnitOfWorkFactory,
) -> None:
    g1, g2, other_version, other_doc = (
        make(),
        make(generation=2),
        make(version=V2),
        make(document=D2, version=VersionRef(UUID(int=0x77))),
    )
    for run in (g1, g2, other_version, other_doc):
        put(factory, run)
    with factory.begin(SYSTEM_A) as uow:
        assert uow.runs.get_for_version(V1, 1) == g1
        assert uow.runs.get_for_version(V1, 2) == g2
        assert uow.runs.get_for_version(V1, 3) is None
        assert uow.runs.latest_for_version(V1) == g2
        assert uow.runs.latest_for_version(VersionRef(UUID(int=0x1234))) is None
        by_doc = uow.runs.list_for_document(D1)
        assert {r.id for r in by_doc} == {g1.id, g2.id, other_version.id}
        assert [r.id for r in uow.runs.list_for_document(D2)] == [other_doc.id]


def step(factory: IngestionUnitOfWorkFactory, run: IngestionRun, *actions) -> None:  # type: ignore[no-untyped-def]
    """Load, apply, save, commit: one transaction per transition, as the engine does it."""
    with factory.begin(ctx(run.tenant_id)) as uow:
        loaded = uow.runs.get(run.id, for_update=True)
        assert loaded is not None
        for action in actions:
            action(loaded)
        uow.runs.save(loaded)
        uow.commit()


def test_save_persists_run_and_stage_state(factory: IngestionUnitOfWorkFactory) -> None:
    run = make()
    put(factory, run)
    step(factory, run, lambda r: r.begin_stage("scan", NOW))
    step(factory, run, lambda r: r.succeed_stage("scan", NOW + timedelta(milliseconds=250)))
    step(factory, run, lambda r: r.begin_stage("alpha", NOW))
    step(factory, run, lambda r: r.retry_stage("alpha", "busy", NOW))
    after = fetch(factory, TENANT_A, run)
    assert after is not None
    scan, alpha = after.stages
    assert (scan.status, scan.attempts, scan.duration_ms) == (StageStatus.SUCCEEDED, 1, 250)
    assert (alpha.status, alpha.attempts, alpha.error_code) == (StageStatus.PENDING, 1, "busy")
    assert after.progress_at == NOW and after.status is RunStatus.RUNNING


def test_terminal_state_and_bookkeeping_persist(factory: IngestionUnitOfWorkFactory) -> None:
    run = make()
    put(factory, run)
    step(factory, run, lambda r: r.begin_stage("scan", NOW))
    step(factory, run, lambda r: r.quarantine_stage("scan", "malware_detected", NOW))
    step(factory, run, lambda r: r.record_redelivery(NOW), lambda r: r.acknowledge(NOW))
    after = fetch(factory, TENANT_A, run)
    assert after is not None
    assert (after.status, after.error_code, after.redeliveries) == (
        RunStatus.QUARANTINED,
        "malware_detected",
        1,
    )
    assert after.acknowledged_at == NOW and after.finished_at == NOW


def test_saving_a_run_that_was_never_added_fails(factory: IngestionUnitOfWorkFactory) -> None:
    with pytest.raises(InfrastructureError), factory.begin(SYSTEM_A) as uow:
        uow.runs.save(make())


def test_adding_is_insert_if_absent_and_never_overwrites(
    factory: IngestionUnitOfWorkFactory,
) -> None:
    original = make()
    with factory.begin(SYSTEM_A) as uow:
        assert uow.runs.add(original) is True
        uow.commit()
    progressed = make()
    progressed.begin_stage("scan", NOW)
    with factory.begin(SYSTEM_A) as uow:
        assert uow.runs.add(progressed) is False  # same id: refused, nothing written
        assert uow.runs.add(make(generation=2)) is True  # a different generation is a new run
        uow.commit()
    assert fetch(factory, TENANT_A, original) == original


def test_tenants_are_isolated_and_a_run_of_another_tenant_cannot_be_written(
    factory: IngestionUnitOfWorkFactory,
) -> None:
    a, b = make(TENANT_A), make(TENANT_B)
    put(factory, a)
    put(factory, b)  # same version id, other tenant: independent
    assert a.id != b.id
    with factory.begin(SYSTEM_B) as uow:
        assert uow.runs.get(a.id) is None
        assert uow.runs.get_for_version(V1, 1) == b
        assert uow.runs.latest_for_version(V1) == b
        assert [r.id for r in uow.runs.list_for_document(D1)] == [b.id]
        assert uow.runs.metrics().runs_by_status == (("running", 1),)
    with pytest.raises(InfrastructureError), factory.begin(SYSTEM_B) as uow:
        uow.runs.add(a)  # a tenant-A run through tenant B's unit of work
    with pytest.raises(InfrastructureError), factory.begin(SYSTEM_B) as uow:
        uow.runs.save(a)


def test_remove_for_document_deletes_runs_and_their_stages_in_one_tenant_only(
    factory: IngestionUnitOfWorkFactory,
) -> None:
    for run in (make(), make(generation=2), make(document=D2, version=V2), make(TENANT_B)):
        put(factory, run)
    with factory.begin(SYSTEM_A) as uow:
        assert uow.runs.remove_for_document(D1) == 2
        assert uow.runs.remove_for_document(D1) == 0
        uow.commit()
    with factory.begin(SYSTEM_A) as uow:
        assert [r.document for r in uow.runs.list_for_document(D2)] == [D2]
        assert uow.runs.list_for_document(D1) == ()
    with factory.begin(SYSTEM_B) as uow:
        assert len(uow.runs.list_for_document(D1)) == 1


def test_metrics_match_across_adapters(factory: IngestionUnitOfWorkFactory) -> None:
    done, failed = make(), make(version=V2)
    dead = make(version=VersionRef(UUID(int=0x70)))
    done_too = make(version=VersionRef(UUID(int=0x71)))
    for run in (done, failed, dead, done_too):
        put(factory, run)
    ms = timedelta(milliseconds=1)
    step(factory, done, lambda r: r.begin_stage("scan", NOW))
    step(factory, done, lambda r: r.succeed_stage("scan", NOW + 100 * ms))
    step(factory, done, lambda r: r.begin_stage("alpha", NOW))
    step(factory, done, lambda r: r.retry_stage("alpha", "busy", NOW))
    step(factory, done, lambda r: r.begin_stage("alpha", NOW))
    step(
        factory, done, lambda r: r.succeed_stage("alpha", NOW + 300 * ms), lambda r: r.complete(NOW)
    )
    step(factory, failed, lambda r: r.begin_stage("scan", NOW))
    step(factory, failed, lambda r: r.fail_stage("scan", "bad", NOW + 500 * ms))
    step(factory, failed, lambda r: r.acknowledge(NOW))
    step(factory, dead, lambda r: r.dead_letter("stalled", NOW))
    step(factory, done_too, lambda r: r.begin_stage("scan", NOW))
    step(factory, done_too, lambda r: r.succeed_stage("scan", NOW))
    step(factory, done_too, lambda r: r.begin_stage("alpha", NOW))
    step(factory, done_too, lambda r: r.succeed_stage("alpha", NOW), lambda r: r.complete(NOW))
    with factory.begin(SYSTEM_A) as uow:
        m = uow.runs.metrics()
    assert dict(m.runs_by_status) == {"completed": 2, "failed": 1, "dead_lettered": 1}
    assert (m.dead_lettered_runs, m.unacknowledged_outcomes) == (1, 2)  # 2 unacked, 1 acked
    stages = {s.stage: s for s in m.stages}
    assert (stages["scan"].runs, stages["scan"].retries, stages["scan"].failures) == (3, 0, 1)
    assert (stages["scan"].average_duration_ms, stages["scan"].maximum_duration_ms) == (50, 100)
    assert (stages["alpha"].runs, stages["alpha"].retries, stages["alpha"].failures) == (2, 1, 0)
    assert (stages["alpha"].average_duration_ms, stages["alpha"].maximum_duration_ms) == (150, 300)


def test_metrics_of_an_empty_tenant_are_empty(factory: IngestionUnitOfWorkFactory) -> None:
    with factory.begin(SYSTEM_A) as uow:
        m = uow.runs.metrics()
    assert (m.runs_by_status, m.dead_lettered_runs, m.unacknowledged_outcomes, m.stages) == (
        (),
        0,
        0,
        (),
    )


def test_run_ids_are_the_deterministic_ones(factory: IngestionUnitOfWorkFactory) -> None:
    put(factory, make())
    with factory.begin(SYSTEM_A) as uow:
        loaded = uow.runs.get_for_version(V1, 1)
    assert loaded is not None and loaded.id == run_id_for(TENANT_A, V1, 1)


def test_two_runs_with_different_ids_cannot_share_a_version_and_generation(
    factory: IngestionUnitOfWorkFactory,
) -> None:
    """The deterministic id already prevents it; the store enforces it independently."""
    first = make()
    impostor = make()
    impostor.id = type(first.id)(UUID(int=0xFEED))
    with factory.begin(SYSTEM_A) as uow:
        assert uow.runs.add(first) is True
        assert uow.runs.add(impostor) is False
        uow.commit()
    with factory.begin(SYSTEM_A) as uow:
        assert uow.runs.get(impostor.id) is None
        assert uow.runs.get(first.id) == first
