"""Tenant scope, SystemContext semantics, transaction lifecycle and the transactional UoW."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.queue import PostgresJobQueue
from lda.platform.database.transaction import SystemQueueTransaction, TenantTransaction
from lda.platform.database.unit_of_work import SqlUnitOfWork, SqlUnitOfWorkFactory
from lda.platform.testing.doubles import FakeClock, InMemoryAuditRecorder, SequentialIdGenerator
from lda.shared.kernel.context import ExecutionContext, SystemContext, TenantContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import PrincipalId, RequestId
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.jobs import JobSpec
from platform_support import enqueue_job, job_rows, make_wiring, tenant

LEASE = timedelta(seconds=30)
SPEC = JobSpec(queue="q", job_type="t")


def user(n: int) -> TenantContext:
    return TenantContext(tenant(n), PrincipalId(UUID(int=7)), RequestId(UUID(int=8)))


def system(n: int) -> SystemContext:
    return SystemContext(tenant(n), "test:system")


def scope(uow: SqlUnitOfWork) -> tuple[str | None, str | None]:
    row = uow.transaction.session.execute(
        text(
            "SELECT current_setting('app.tenant_id', true), current_setting('app.system_scope', true)"
        )
    ).one()
    return row[0], row[1]


def visible_tenants(uow: SqlUnitOfWork) -> set[UUID]:
    rows = uow.transaction.session.execute(text("SELECT DISTINCT tenant_id FROM platform.jobs"))
    return {r[0] for r in rows}


@pytest.fixture
def factory(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> SqlUnitOfWorkFactory:
    return make_wiring(app_engine, clock, ids).factory


@pytest.fixture
def two_tenants(app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator) -> None:
    for n in (1, 2):
        enqueue_job(app_engine, clock, ids, n, JobSpec(queue="q", job_type=f"t{n}"))


# ---- SystemContext -> tenant-bound transaction -------------------------------------------------


@pytest.mark.parametrize("ctx_for", [user, system], ids=["user", "system"])
def test_both_context_kinds_bind_exactly_their_tenant_and_no_system_scope(
    factory: SqlUnitOfWorkFactory, ctx_for: Callable[[int], ExecutionContext]
) -> None:
    ctx = ctx_for(1)
    with factory.begin(ctx) as uow:
        tenant_guc, system_guc = scope(uow)
        assert tenant_guc == str(tenant(1)) and uow.tenant_id == tenant(1)
        assert system_guc in (None, "")  # a SystemContext does NOT unlock the queue-wide policy


def test_system_context_sees_and_changes_only_its_own_tenants_rows(
    factory: SqlUnitOfWorkFactory, admin_engine: Engine, two_tenants: None
) -> None:
    with factory.begin(system(1)) as uow:
        assert visible_tenants(uow) == {tenant(1).value}
        updated = uow.transaction.execute_count(
            text("UPDATE platform.jobs SET status = 'dead'"), {}
        )
        assert updated == 1  # the other tenant's row is invisible to a blanket UPDATE
        deleted_other = uow.transaction.session.execute(
            text("UPDATE platform.jobs SET status = 'dead' WHERE tenant_id = :t"),
            {"t": tenant(2).value},
        )
        assert deleted_other.rowcount == 0  # type: ignore[attr-defined]
        uow.commit()
    by_tenant = {r.tenant_id: r.status for r in job_rows(admin_engine)}
    assert by_tenant == {tenant(1).value: "dead", tenant(2).value: "queued"}


def test_system_context_cannot_write_rows_for_another_tenant(factory: SqlUnitOfWorkFactory) -> None:
    with pytest.raises(DBAPIError), factory.begin(system(1)) as uow:
        uow.transaction.session.execute(
            text(
                "INSERT INTO platform.jobs (id, tenant_id, queue, job_type, payload, status, attempts, "
                "max_attempts, run_at, created_at, updated_at) VALUES (gen_random_uuid(), :t, 'q', 'x', "
                "'{}', 'queued', 0, 1, now(), now(), now())"
            ),
            {"t": tenant(2).value},
        )


def test_jobs_enqueued_by_system_context_belong_to_its_tenant_only(
    factory: SqlUnitOfWorkFactory, admin_engine: Engine
) -> None:
    with factory.begin(system(3)) as uow:
        assert uow.jobs.enqueue(SPEC) is not None
        uow.commit()
    assert [r.tenant_id for r in job_rows(admin_engine)] == [tenant(3).value]


def test_only_the_queue_transaction_has_the_cross_tenant_view(
    app_engine: Engine, two_tenants: None
) -> None:
    with SystemQueueTransaction(app_engine) as sys_tx:
        rows = sys_tx.session.execute(text("SELECT DISTINCT tenant_id FROM platform.jobs"))
        assert {r[0] for r in rows} == {tenant(1).value, tenant(2).value}
    with TenantTransaction(app_engine, tenant(1)) as tx:
        rows = tx.session.execute(text("SELECT DISTINCT tenant_id FROM platform.jobs"))
        assert {r[0] for r in rows} == {tenant(1).value}


def test_job_queue_is_bound_to_the_transaction_tenant_not_to_the_spec(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    with TenantTransaction(app_engine, tenant(1)) as tx:
        PostgresJobQueue(tx, clock, ids).enqueue(
            JobSpec(queue="q", job_type="t", payload={"tenant_id": str(tenant(2))})
        )
        tx.commit()
    assert [r.tenant_id for r in job_rows(admin_engine)] == [tenant(1).value]


# ---- transaction lifecycle ---------------------------------------------------------------------


def test_a_transaction_object_is_single_use_after_commit(app_engine: Engine) -> None:
    tx = TenantTransaction(app_engine, tenant(1))
    with tx:
        tx.commit()
    with pytest.raises(InfrastructureError, match="transaction_reused"), tx:
        pass


def test_a_transaction_object_is_single_use_after_rollback(app_engine: Engine) -> None:
    tx = TenantTransaction(app_engine, tenant(1))
    with tx:
        tx.rollback()
    with pytest.raises(InfrastructureError, match="transaction_reused"), tx:
        pass


def test_entering_twice_without_finishing_is_rejected(app_engine: Engine) -> None:
    with (
        TenantTransaction(app_engine, tenant(1)) as tx,
        pytest.raises(InfrastructureError, match="transaction_reused"),
    ):
        tx.__enter__()


@pytest.mark.parametrize("first", ["commit", "rollback"])
@pytest.mark.parametrize("second", ["commit", "rollback"])
def test_finishing_twice_is_an_error_not_a_silent_noop(
    app_engine: Engine, first: str, second: str
) -> None:
    with TenantTransaction(app_engine, tenant(1)) as tx:
        getattr(tx, first)()
        with pytest.raises(InfrastructureError, match="transaction_not_open"):
            getattr(tx, second)()


def test_nothing_can_run_after_the_transaction_ended(app_engine: Engine) -> None:
    with TenantTransaction(app_engine, tenant(1)) as tx:
        tx.commit()
        with pytest.raises(InfrastructureError, match="transaction_not_open"):
            tx.execute_count(text("UPDATE platform.jobs SET status = status"), {})
        with pytest.raises(InfrastructureError, match="transaction_not_open"):
            _ = tx.session


def test_nothing_can_run_before_the_transaction_started(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    tx = TenantTransaction(app_engine, tenant(1))
    with pytest.raises(InfrastructureError, match="transaction_not_open"):
        PostgresJobQueue(tx, clock, ids).enqueue(SPEC)  # no scope has been established yet


def test_leaving_the_block_without_commit_rolls_back(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    with TenantTransaction(app_engine, tenant(1)) as tx:
        PostgresJobQueue(tx, clock, ids).enqueue(SPEC)
    assert job_rows(admin_engine) == []


def test_an_exception_rolls_back_and_propagates(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    with pytest.raises(RuntimeError, match="boom"), TenantTransaction(app_engine, tenant(1)) as tx:
        PostgresJobQueue(tx, clock, ids).enqueue(SPEC)
        raise RuntimeError("boom")
    assert job_rows(admin_engine) == []


@pytest.mark.parametrize("how", ["commit", "rollback", "exception", "leave"])
def test_scope_never_leaks_to_the_next_user_of_a_pooled_connection(
    database_url: str, how: str
) -> None:
    engine = create_engine(database_url, pool_size=1, max_overflow=0)  # one connection, reused
    try:
        try:
            with TenantTransaction(engine, tenant(1)) as tx:
                if how == "commit":
                    tx.commit()
                elif how == "rollback":
                    tx.rollback()
                elif how == "exception":
                    raise RuntimeError("boom")
        except RuntimeError:
            pass
        with engine.connect() as conn:
            leaked = conn.execute(
                text("SELECT current_setting('app.tenant_id', true)")
            ).scalar_one()
            assert leaked in (None, "")
            assert conn.execute(text("SELECT count(*) FROM platform.jobs")).scalar_one() == 0
    finally:
        engine.dispose()


def test_a_failed_enter_leaves_no_usable_session() -> None:
    unreachable = create_engine(
        "postgresql+psycopg://u:p@127.0.0.1:1/db", connect_args={"connect_timeout": 2}
    )
    tx = TenantTransaction(unreachable, tenant(1))
    with pytest.raises(OperationalError), tx:
        pass
    with pytest.raises(InfrastructureError, match="transaction_not_open"):
        _ = tx.session
    unreachable.dispose()


# ---- unit of work ------------------------------------------------------------------------------


def test_unit_of_work_is_single_use(factory: SqlUnitOfWorkFactory) -> None:
    uow = factory.begin(system(1))
    with uow:
        uow.commit()
        with pytest.raises(InfrastructureError, match="transaction_not_open"):
            uow.commit()
    with pytest.raises(InfrastructureError, match="transaction_reused"), uow:
        pass


def test_unit_of_work_services_are_unusable_before_entering(factory: SqlUnitOfWorkFactory) -> None:
    uow = factory.begin(system(1))
    with pytest.raises(InfrastructureError, match="transaction_not_open"):
        uow.jobs.enqueue(SPEC)


def test_enqueue_commits_and_rolls_back_with_the_unit_of_work(
    factory: SqlUnitOfWorkFactory, admin_engine: Engine
) -> None:
    with factory.begin(system(1)) as uow:
        uow.jobs.enqueue(JobSpec(queue="q", job_type="kept"))
        uow.commit()
    with factory.begin(system(1)) as uow:
        uow.jobs.enqueue(JobSpec(queue="q", job_type="explicit_rollback"))
        uow.rollback()
    with pytest.raises(RuntimeError), factory.begin(system(1)) as uow:
        uow.jobs.enqueue(JobSpec(queue="q", job_type="failed_use_case"))
        raise RuntimeError("business rule failed after enqueue")
    with factory.begin(system(1)) as uow:
        uow.jobs.enqueue(JobSpec(queue="q", job_type="never_committed"))
    assert [(r.status, r.attempts) for r in job_rows(admin_engine)] == [("queued", 0)]
    with admin_engine.connect() as conn:
        types = conn.execute(text("SELECT job_type FROM platform.jobs")).scalars().all()
    assert types == ["kept"]


def test_a_job_is_invisible_to_workers_until_the_enqueuing_transaction_commits(
    app_engine: Engine, factory: SqlUnitOfWorkFactory, clock: FakeClock
) -> None:
    store = JobLeaseStore(app_engine, clock)
    with factory.begin(system(1)) as uow:
        uow.jobs.enqueue(SPEC)
        assert store.claim("q", 5, "w", LEASE) == []  # separate connection: no dirty read
        uow.commit()
    assert len(store.claim("q", 5, "w", LEASE)) == 1


# ---- audit cannot silently disappear -----------------------------------------------------------


def fact(n: int) -> AuditFact:
    return AuditFact(
        tenant_id=tenant(n),
        actor=None,
        action="x.did",
        resource_type="x",
        resource_id=None,
        outcome=AuditOutcome.SUCCESS,
        request_id=None,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class FailingAudit:
    def record(self, fact: AuditFact) -> None:
        raise InfrastructureError("audit_unavailable")


def test_audit_failure_fails_the_action_and_rolls_back_its_effects(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    factory = make_wiring(app_engine, clock, ids, lambda tx: FailingAudit()).factory
    with (
        pytest.raises(InfrastructureError, match="audit_unavailable"),
        factory.begin(user(1)) as uow,
    ):
        uow.jobs.enqueue(SPEC)
        uow.audit.record(fact(1))  # fail-closed: the exception reaches the caller
        uow.commit()
    assert job_rows(admin_engine) == []


def test_the_audit_recorder_joins_the_units_own_transaction(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    seen: list[TenantTransaction] = []

    def audit_factory(tx: TenantTransaction) -> InMemoryAuditRecorder:
        seen.append(tx)
        return InMemoryAuditRecorder()

    factory = make_wiring(app_engine, clock, ids, audit_factory).factory
    uow = factory.begin(user(1))
    assert seen == [uow.transaction]  # same transaction => audit commits/rolls back with the action


def test_audit_has_no_default_anywhere_in_the_platform_wiring() -> None:
    from lda.bootstrap.container import build_platform

    for target in (SqlUnitOfWorkFactory.__init__, build_platform):
        param = inspect.signature(target).parameters["audit_factory"]
        assert param.default is inspect.Parameter.empty, f"{target.__qualname__} must not default"
