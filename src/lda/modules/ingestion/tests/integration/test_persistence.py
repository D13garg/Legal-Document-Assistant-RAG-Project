"""Real Postgres: schema ownership, forced RLS, least privilege and the lifecycle triggers."""

from __future__ import annotations

import threading
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.ingestion.application.dto.runs import ExecutionOutcome
from lda.modules.ingestion.application.pipeline import Pipeline
from lda.modules.ingestion.application.ports import (
    ContentScanner,
    ScanRequest,
    ScanVerdict,
    StageOutcome,
    StageRequest,
    StageSucceeded,
)
from lda.modules.ingestion.application.starter import RunStarter
from lda.modules.ingestion.application.use_cases.execute_run import ExecuteRun
from lda.modules.ingestion.infrastructure.persistence.unit_of_work import (
    SqlIngestionUnitOfWorkFactory,
)
from lda.modules.ingestion.testing import ScriptedScanner, ScriptedStage
from lda.modules.ingestion.tests.contract.test_repository_contract import make, put, step
from lda.modules.ingestion.tests.support import (
    DELAY,
    START,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    command,
)
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from platform_support import Database

TABLES = ("runs", "stage_runs")
NOW = START + timedelta(seconds=5)


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def admin(engine: Engine, statement: str, **params: object) -> None:
    with engine.begin() as conn:
        conn.execute(text(statement), params)


def scalar(engine: Engine, statement: str) -> object:
    with engine.connect() as conn:
        return conn.execute(text(statement)).scalar_one()


# ---- schema shape ------------------------------------------------------------------------------


def test_tables_live_only_in_the_ingestion_schema_and_reference_nothing_outside_it(
    admin_engine: Engine,
) -> None:
    with admin_engine.connect() as conn:
        schemas = {
            r[0]: r[1]
            for r in conn.execute(
                text(
                    "SELECT c.relname, n.nspname FROM pg_class c JOIN pg_namespace n "
                    "ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND c.relname = ANY(:t)"
                ),
                {"t": list(TABLES)},
            )
            if r[1] == "ingestion"
        }
        foreign = conn.execute(
            text(
                "SELECT t.relname, n2.nspname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "JOIN pg_class r ON r.oid = c.confrelid "
                "JOIN pg_namespace n2 ON n2.oid = r.relnamespace "
                "WHERE c.contype = 'f' AND n.nspname = 'ingestion'"
            )
        ).all()
    assert schemas == {"runs": "ingestion", "stage_runs": "ingestion"}
    assert {schema for _, schema in foreign} <= {"ingestion"}  # no cross-schema FK


def test_rls_is_enabled_and_forced_on_every_table(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'ingestion' AND c.relkind = 'r'"
            )
        ).all()
    assert {r[0] for r in rows} == set(TABLES)
    assert all(r[1] and r[2] for r in rows)


def test_the_runtime_role_has_no_truncate_ddl_or_other_schema_rights(
    sql_factory: SqlIngestionUnitOfWorkFactory, app_engine: Engine
) -> None:
    for statement in (
        "TRUNCATE ingestion.runs",
        "DROP TABLE ingestion.runs",
        "ALTER TABLE ingestion.runs ADD COLUMN x int",
        "CREATE TABLE ingestion.x (a int)",
    ):
        with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, TENANT_A) as tx:
            tx.session.execute(text(statement))
        assert sqlstate(caught.value) in {"42501", "42P01"}, statement
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(text("SELECT * FROM documents.documents"))
    assert sqlstate(caught.value) in {"42501", "42P01"}


# ---- row level security ------------------------------------------------------------------------


def test_a_tenant_sees_and_changes_only_its_own_rows_even_with_a_hand_written_query(
    sql_factory: SqlIngestionUnitOfWorkFactory, app_engine: Engine
) -> None:
    put(sql_factory, make(TENANT_A))
    put(sql_factory, make(TENANT_B))
    with TenantTransaction(app_engine, TENANT_A) as tx:
        seen = tx.session.execute(text("SELECT tenant_id FROM ingestion.runs")).scalars().all()
        assert [str(t) for t in seen] == [str(TENANT_A.value)]
        stages = tx.session.execute(text("SELECT tenant_id FROM ingestion.stage_runs")).all()
        assert len(stages) == 2
        changed = tx.session.execute(
            text("UPDATE ingestion.runs SET progress_at = now() RETURNING run_id")
        ).all()
        assert len(changed) == 1
        gone = tx.session.execute(text("DELETE FROM ingestion.runs RETURNING run_id")).all()
        assert len(gone) == 1
        tx.commit()
    with TenantTransaction(app_engine, TENANT_A) as tx:
        assert tx.session.execute(text("SELECT count(*) FROM ingestion.runs")).scalar_one() == 0
    with TenantTransaction(app_engine, TENANT_B) as tx:  # tenant B's row was never touched
        assert tx.session.execute(text("SELECT count(*) FROM ingestion.runs")).scalar_one() == 1


def test_inserting_a_row_for_another_tenant_is_rejected(
    sql_factory: SqlIngestionUnitOfWorkFactory, app_engine: Engine
) -> None:
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(
            text(
                "INSERT INTO ingestion.runs (tenant_id, run_id, document_id, version_id, "
                "generation, status, created_at, progress_at, redeliveries) VALUES "
                "(:t, :r, :d, :v, 1, 'running', now(), now(), 0)"
            ),
            {
                "t": str(TENANT_B.value),
                "r": str(UUID(int=1)),
                "d": str(UUID(int=2)),
                "v": str(UUID(int=3)),
            },
        )
    assert sqlstate(caught.value) == "42501"


def test_without_a_tenant_nothing_is_visible(
    sql_factory: SqlIngestionUnitOfWorkFactory, admin_engine: Engine, app_engine: Engine
) -> None:
    put(sql_factory, make())
    with app_engine.connect() as conn:  # a raw connection that never set app.tenant_id
        assert conn.execute(text("SELECT count(*) FROM ingestion.runs")).scalar_one() == 0


# ---- lifecycle triggers (the database repeats the state machine) -------------------------------


def run_sql(app_engine: Engine, sql: str, tenant=TENANT_A, **params: object) -> None:  # type: ignore[no-untyped-def]
    with TenantTransaction(app_engine, tenant) as tx:
        tx.session.execute(text(sql), params)
        tx.commit()


def expect_blocked(app_engine: Engine, sql: str, state: str = "55000", **params: object) -> None:
    with pytest.raises(DBAPIError) as caught:
        run_sql(app_engine, sql, **params)
    assert sqlstate(caught.value) == state, sql


CLOSE = "UPDATE ingestion.runs SET status = '{}', finished_at = now()"
REOPEN = "UPDATE ingestion.runs SET status = 'running', finished_at = NULL"


@pytest.mark.parametrize(
    ("setup", "forbidden"),
    [
        (CLOSE.format("completed"), REOPEN),
        (CLOSE.format("failed"), "UPDATE ingestion.runs SET status = 'completed'"),
        (CLOSE.format("quarantined"), "UPDATE ingestion.runs SET status = 'cancelled'"),
        (CLOSE.format("cancelled"), REOPEN),
        (CLOSE.format("dead_lettered"), "UPDATE ingestion.runs SET status = 'completed'"),
        (CLOSE.format("dead_lettered"), REOPEN),
    ],
)
def test_the_database_refuses_every_run_transition_the_domain_forbids(
    sql_factory: SqlIngestionUnitOfWorkFactory,
    app_engine: Engine,
    setup: str,
    forbidden: str,
) -> None:
    put(sql_factory, make())
    run_sql(app_engine, setup)
    expect_blocked(app_engine, forbidden)


def test_the_allowed_run_transitions_pass(
    sql_factory: SqlIngestionUnitOfWorkFactory, app_engine: Engine
) -> None:
    put(sql_factory, make())
    run_sql(app_engine, "UPDATE ingestion.runs SET status = 'dead_lettered', finished_at = now()")
    run_sql(
        app_engine, "UPDATE ingestion.runs SET status = 'cancelled'"
    )  # dead_lettered -> cancelled


def test_a_finished_run_keeps_its_code_and_time_but_bookkeeping_may_still_change(
    sql_factory: SqlIngestionUnitOfWorkFactory, app_engine: Engine
) -> None:
    put(sql_factory, make())
    run_sql(
        app_engine,
        "UPDATE ingestion.runs SET status = 'failed', error_code = 'bad', finished_at = now()",
    )
    expect_blocked(app_engine, "UPDATE ingestion.runs SET error_code = 'other'")
    expect_blocked(app_engine, "UPDATE ingestion.runs SET finished_at = now() + interval '1 day'")
    run_sql(app_engine, "UPDATE ingestion.runs SET redeliveries = 1, progress_at = now()")
    run_sql(app_engine, "UPDATE ingestion.runs SET acknowledged_at = now()")
    expect_blocked(app_engine, "UPDATE ingestion.runs SET acknowledged_at = NULL")
    expect_blocked(app_engine, "UPDATE ingestion.runs SET redeliveries = 0")


def test_run_identity_is_immutable(
    sql_factory: SqlIngestionUnitOfWorkFactory, app_engine: Engine
) -> None:
    put(sql_factory, make())
    for sql in (
        "UPDATE ingestion.runs SET document_id = gen_random_uuid()",
        "UPDATE ingestion.runs SET version_id = gen_random_uuid()",
        "UPDATE ingestion.runs SET run_id = gen_random_uuid()",
        "UPDATE ingestion.runs SET generation = 2",
        "UPDATE ingestion.runs SET created_at = now()",
    ):
        expect_blocked(app_engine, sql)


def test_check_constraints_reject_impossible_rows(
    sql_factory: SqlIngestionUnitOfWorkFactory, app_engine: Engine
) -> None:
    put(sql_factory, make())
    for sql in (
        "UPDATE ingestion.runs SET error_code = 'Has Space'",
        "UPDATE ingestion.runs SET status = 'completed'",  # finished_at must be set
        "UPDATE ingestion.runs SET acknowledged_at = now()",  # not an outcome yet
        "UPDATE ingestion.stage_runs SET error_code = 'secret text here'",
    ):
        expect_blocked(app_engine, sql, state="23514")


STAGE = "UPDATE ingestion.stage_runs SET {} WHERE stage = 'scan'"
STARTED = STAGE.format("status = 'running', attempts = 1")


@pytest.mark.parametrize(
    ("setup", "forbidden"),
    [
        ([], STAGE.format("status = 'succeeded'")),  # pending -> succeeded skips running
        ([], STAGE.format("status = 'failed'")),
        ([STARTED], STAGE.format("status = 'running', attempts = 0")),
        ([STARTED, STAGE.format("status = 'succeeded'")], STAGE.format("status = 'running'")),
        ([STARTED, STAGE.format("status = 'succeeded'")], STAGE.format("attempts = 5")),
        ([STARTED, STAGE.format("status = 'failed'")], STAGE.format("status = 'pending'")),
        ([STARTED, STAGE.format("status = 'quarantined'")], STAGE.format("error_code = 'x'")),
    ],
)
def test_the_database_refuses_every_stage_transition_the_domain_forbids(
    sql_factory: SqlIngestionUnitOfWorkFactory,
    app_engine: Engine,
    setup: list[str],
    forbidden: str,
) -> None:
    put(sql_factory, make())
    for statement in setup:
        run_sql(app_engine, statement)
    expect_blocked(app_engine, forbidden)


def test_stage_identity_is_immutable_and_a_running_stage_may_restart(
    sql_factory: SqlIngestionUnitOfWorkFactory, app_engine: Engine
) -> None:
    put(sql_factory, make())
    expect_blocked(
        app_engine, "UPDATE ingestion.stage_runs SET stage = 'renamed' WHERE stage = 'scan'"
    )
    expect_blocked(app_engine, "UPDATE ingestion.stage_runs SET position = 7 WHERE stage = 'scan'")
    run_sql(
        app_engine,
        "UPDATE ingestion.stage_runs SET status='running', attempts=1 WHERE stage='scan'",
    )
    run_sql(
        app_engine,
        "UPDATE ingestion.stage_runs SET status='running', attempts=2 WHERE stage='scan'",
    )
    run_sql(app_engine, "UPDATE ingestion.stage_runs SET status='pending' WHERE stage='scan'")


# ---- real concurrency and crashes --------------------------------------------------------------


class Crash(BaseException):
    """A process dying: not an `Exception`, so nothing in the engine may swallow it."""


class BarrierScanner:
    """Every scan waits for the other executor, so both are inside the same stage at once."""

    def __init__(self) -> None:
        self.barrier = threading.Barrier(2, timeout=20)
        self.calls = 0
        self._lock = threading.Lock()

    def scan(self, request: ScanRequest) -> ScanVerdict:
        with self._lock:
            self.calls += 1
        self.barrier.wait()
        return ScanVerdict.CLEAN


def engine_for(
    sql_factory: SqlIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    stages: list[ScriptedStage],
    scanner: ContentScanner | None = None,
) -> ExecuteRun:
    pipeline = Pipeline(scanner or ScriptedScanner(), stages)
    return ExecuteRun(sql_factory, pipeline, RunStarter(pipeline, DELAY), clock, ids)


def seed_job(admin_engine: Engine, job: int, token: int) -> None:
    """The fence needs a real running job row, which a worker would have claimed."""
    admin(
        admin_engine,
        "INSERT INTO platform.jobs (id, tenant_id, queue, job_type, payload, status, attempts, "
        "max_attempts, run_at, lease_owner, lease_expires_at, fencing_token, created_at, "
        "updated_at) VALUES (:id, :t, 'ingestion', 'ingestion:run', '{}', 'running', 1, 5, "
        "now(), 'w', now() + interval '10 years', :tok, now(), now())",
        id=str(UUID(int=job)),
        t=str(TENANT_A.value),
        tok=token,
    )


def event_jobs(admin_engine: Engine) -> int:
    with admin_engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM platform.jobs WHERE job_type LIKE 'event:%'")
            ).scalar_one()
        )


def test_two_concurrent_executors_of_the_same_run_leave_exactly_one_outcome(
    sql_factory: SqlIngestionUnitOfWorkFactory,
    admin_engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    """Both run the stage at the same time (a lease was lost and the job re-claimed). The one that
    started the stage LAST owns it; the other's result is discarded by the compare-and-set."""
    seed_job(admin_engine, 0x7001, 1)
    seed_job(admin_engine, 0x7002, 1)
    scanner = BarrierScanner()
    stage = ScriptedStage("fake_one")
    executor = engine_for(sql_factory, clock, ids, [stage], scanner)
    results: list[ExecutionOutcome] = []
    errors: list[BaseException] = []

    def go(job: int) -> None:
        try:
            results.append(executor.execute(SYSTEM_A, command(job=0x7000 + job, token=1)).outcome)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(j,)) for j in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert sorted(results) == [ExecutionOutcome.FINISHED, ExecutionOutcome.INACTIVE]
    assert scanner.calls == 2 and len(stage.calls) == 1  # the loser never got past the scan
    assert scalar(admin_engine, "SELECT status FROM ingestion.runs") == "completed"
    assert event_jobs(admin_engine) == 1  # one durable outcome, however many executors ran
    assert (
        scalar(admin_engine, "SELECT attempts FROM ingestion.stage_runs WHERE stage = 'scan'") == 2
    )


def test_a_process_death_between_the_transactions_resumes_without_a_duplicate_outcome(
    sql_factory: SqlIngestionUnitOfWorkFactory,
    admin_engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    seed_job(admin_engine, 0x7001, 1)
    stage = ScriptedStage("fake_one", [Crash()])
    engine = engine_for(sql_factory, clock, ids, [stage])
    with pytest.raises(Crash):
        engine.execute(SYSTEM_A, command(job=0x7001, token=1))
    with admin_engine.connect() as conn:  # the committed state shows exactly where it died
        rows = {
            r[0]: (r[1], r[2])
            for r in conn.execute(text("SELECT stage, status, attempts FROM ingestion.stage_runs"))
        }
    assert rows == {"scan": ("succeeded", 1), "fake_one": ("running", 1)}
    assert scalar(admin_engine, "SELECT status FROM ingestion.runs") == "running"
    assert event_jobs(admin_engine) == 0
    # restart: the queue delivers the job again
    result = engine.execute(SYSTEM_A, command(job=0x7001, token=1, attempt=2))
    assert result.outcome is ExecutionOutcome.FINISHED
    assert scalar(admin_engine, "SELECT status FROM ingestion.runs") == "completed"
    assert len(stage.calls) == 2 and event_jobs(admin_engine) == 1
    # a duplicate delivery after completion is inert
    jobs = scalar(admin_engine, "SELECT count(*) FROM platform.jobs")
    again = engine.execute(SYSTEM_A, command(job=0x7001, token=1, attempt=3))
    assert again.outcome is ExecutionOutcome.INACTIVE
    assert scalar(admin_engine, "SELECT count(*) FROM platform.jobs") == jobs
    assert len(stage.calls) == 2


def test_a_stale_executor_cannot_commit_after_the_job_was_taken_over(
    sql_factory: SqlIngestionUnitOfWorkFactory,
    admin_engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    from lda.shared.kernel.errors import ConflictError

    seed_job(admin_engine, 0x7001, 1)

    def takeover(request: StageRequest) -> StageOutcome:
        admin(admin_engine, "UPDATE platform.jobs SET fencing_token = 2")  # another worker claims
        return StageSucceeded()

    stage = ScriptedStage("fake_one", [takeover])
    engine = engine_for(sql_factory, clock, ids, [stage])
    with pytest.raises(ConflictError, match="job_lease_lost"):
        engine.execute(SYSTEM_A, command(job=0x7001, token=1))
    with admin_engine.connect() as conn:
        statuses = {
            r[0]: r[1] for r in conn.execute(text("SELECT stage, status FROM ingestion.stage_runs"))
        }
    assert statuses["fake_one"] == "running"  # the stale success was NOT committed
    assert event_jobs(admin_engine) == 0
    result = engine.execute(
        SYSTEM_A, command(job=0x7001, token=2, attempt=2)
    )  # the new owner finishes
    assert result.outcome is ExecutionOutcome.FINISHED
    assert scalar(admin_engine, "SELECT status FROM ingestion.runs") == "completed"
    assert event_jobs(admin_engine) == 1


def test_a_stale_token_cannot_even_start_a_stage(
    sql_factory: SqlIngestionUnitOfWorkFactory,
    admin_engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    from lda.shared.kernel.errors import ConflictError

    seed_job(admin_engine, 0x7001, 2)
    stage = ScriptedStage("fake_one")
    engine = engine_for(sql_factory, clock, ids, [stage])
    with pytest.raises(ConflictError, match="job_lease_lost"):
        engine.execute(SYSTEM_A, command(job=0x7001, token=1))  # holds token 1, the job is at 2
    assert stage.calls == []
    assert scalar(admin_engine, "SELECT count(*) FROM ingestion.runs") == 0  # not even the run


# ---- the repository filters by tenant itself, not only through RLS -----------------------------


def test_every_repository_query_carries_its_own_tenant_predicate(
    sql_factory: SqlIngestionUnitOfWorkFactory, database: Database, admin_engine: Engine
) -> None:
    """A superuser bypasses RLS. Through such a connection a tenant-B repository must still see
    nothing of tenant A, proving the SQL itself is tenant-bound (defence in depth)."""
    from sqlalchemy import create_engine

    from lda.modules.ingestion.domain.ids import DocumentRef
    from lda.modules.ingestion.infrastructure.persistence.repositories import SqlRunRepository
    from lda.modules.ingestion.tests.support import D1

    a = make(TENANT_A)
    put(sql_factory, a)
    # tenant A's run is complete and unacknowledged, with finished stages
    step(sql_factory, a, lambda r: r.begin_stage("scan", NOW))
    step(sql_factory, a, lambda r: r.succeed_stage("scan", NOW + timedelta(milliseconds=40)))
    step(sql_factory, a, lambda r: r.begin_stage("alpha", NOW))
    step(sql_factory, a, lambda r: r.succeed_stage("alpha", NOW), lambda r: r.complete(NOW))
    put(sql_factory, make(TENANT_B, generation=2))  # same version, other tenant
    superuser = create_engine(database.admin_url)
    try:
        with TenantTransaction(superuser, TENANT_B) as tx:
            repo = SqlRunRepository(tx)
            assert repo.get(a.id) is None
            assert repo.get_for_version(a.version, 1) is None  # B has only generation 2
            latest = repo.latest_for_version(a.version)
            assert latest is not None and latest.tenant_id == TENANT_B and latest.generation == 2
            assert [r.tenant_id for r in repo.list_for_document(D1)] == [TENANT_B]
            assert [r.tenant_id for r in repo.list_for_document(DocumentRef(a.document.value))] == [
                TENANT_B
            ]
            seen = repo.metrics()
            assert dict(seen.runs_by_status) == {"running": 1}
            assert seen.unacknowledged_outcomes == 0  # A's unacknowledged outcome is not B's
            assert [(s.stage, s.runs, s.maximum_duration_ms) for s in seen.stages] == [
                ("alpha", 0, None),
                ("scan", 0, None),
            ]
            assert repo.remove_for_document(D1) == 1  # only B's own run
            tx.commit()
        with admin_engine.connect() as conn:
            left = conn.execute(text("SELECT tenant_id FROM ingestion.runs")).scalars().all()
            assert [str(t) for t in left] == [str(TENANT_A.value)]
            stages = conn.execute(text("SELECT count(*) FROM ingestion.stage_runs")).scalar_one()
            assert stages == 2  # tenant A's stage rows survived tenant B's purge
        with TenantTransaction(superuser, TENANT_B) as tx, pytest.raises(Exception, match="tenant"):
            SqlRunRepository(tx).save(a)  # tenant A's run through tenant B's repository
    finally:
        superuser.dispose()
