"""Real Postgres: schema ownership, forced RLS, least privilege, immutability, the fence and the
lifecycle lock that serializes a stage with the purge."""

from __future__ import annotations

import threading
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.chunking.api import ChunkStage, PurgeChunks, PurgeCommand
from lda.modules.chunking.domain.ids import VersionId
from lda.modules.chunking.domain.record import PurgeReceipt
from lda.modules.chunking.infrastructure.persistence.unit_of_work import (
    SqlChunkingUnitOfWorkFactory,
)
from lda.modules.chunking.tests.contract.test_repository_contract import (
    D1,
    NOW,
    V1,
    A,
    B,
    record,
)
from lda.modules.chunking.tests.support import PARAMS, SYSTEM_A, TENANT_A, Rig
from lda.modules.ingestion.api import StageOutcome, StagePermanentFailure, StageSucceeded
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import ConflictError

TABLES = ("chunk_sets", "purge_receipts")
ROLE = "lda_app_chunking"
HASH = "a" * 64
GOOD_ROW = (
    "INSERT INTO chunking.chunk_sets (tenant_id, version_id, document_id, extraction_hash, "
    "chunker_version, content_hash, artifact_bytes, chunk_count, character_count, created_at) "
    "VALUES (:t, :v, :d, :eh, :cv, :h, :ab, :cc, :ch, now())"
)


def row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "t": str(A.value),
        "v": str(V1.value),
        "d": str(D1.value),
        "eh": HASH,
        "cv": "chunk-1; max=2000; min=200",
        "h": HASH,
        "ab": 10,
        "cc": 1,
        "ch": 5,
    }
    return base | over


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def admin(engine: Engine, statement: str, **params: object) -> None:
    with engine.begin() as conn:
        conn.execute(text(statement), params)


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        query = f"SELECT count(*) FROM chunking.{table}"  # noqa: S608
        return int(conn.execute(text(query)).scalar_one())


def test_tables_live_only_in_the_schema_and_have_no_foreign_key_out(admin_engine: Engine) -> None:
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
        }
        foreign = conn.execute(
            text(
                "SELECT t.relname FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE c.contype = 'f' AND t.relnamespace = 'chunking'::regnamespace"
            )
        ).all()
    assert schemas == dict.fromkeys(TABLES, "chunking")
    assert foreign == []


def test_every_table_has_forced_rls_a_tenant_policy_and_a_tenant_leading_key(
    admin_engine: Engine,
) -> None:
    with admin_engine.connect() as conn:
        for table in TABLES:
            flags = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = CAST(:t AS regclass)"
                ),
                {"t": f"chunking.{table}"},
            ).one()
            policy = conn.execute(
                text(
                    "SELECT qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'chunking' AND tablename = :t"
                ),
                {"t": table},
            ).all()
            leading = conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                    "ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
                    "WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary"
                ),
                {"t": f"chunking.{table}"},
            ).scalar_one()
            assert tuple(flags) == (True, True), table
            assert len(policy) == 1 and all("app.tenant_id" in (p or "") for p in policy[0]), table
            assert leading == "tenant_id", table


def test_the_runtime_role_holds_exactly_the_approved_privileges(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'chunking' AND grantee = :r"
            ),
            {"r": ROLE},
        ).all()
    granted: dict[str, set[str]] = {}
    for table, privilege in rows:
        granted.setdefault(table, set()).add(privilege)
    assert granted == {
        "chunk_sets": {"SELECT", "INSERT", "DELETE"},  # never UPDATE: immutable
        "purge_receipts": {"SELECT", "INSERT"},
    }


def test_the_database_itself_enforces_tenant_scope(
    sql_factory: SqlChunkingUnitOfWorkFactory, app_engine: Engine
) -> None:
    with sql_factory.begin(SYSTEM_A) as uow:
        uow.chunk_sets.add(record())
        uow.commit()
    with TenantTransaction(app_engine, B) as tx:
        for table in TABLES:
            assert (
                tx.session.execute(text(f"SELECT count(*) FROM chunking.{table}")).scalar_one()  # noqa: S608
                == 0
            )
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, B) as tx:
        tx.session.execute(text(GOOD_ROW), row())  # tenant A's row under tenant B's context
    assert sqlstate(caught.value) == "42501"
    with app_engine.connect() as conn:  # no tenant context at all: nothing is visible
        assert conn.execute(text("SELECT count(*) FROM chunking.chunk_sets")).scalar_one() == 0


def test_chunk_sets_are_immutable_for_every_role(
    sql_factory: SqlChunkingUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:
    with sql_factory.begin(SYSTEM_A) as uow:
        uow.chunk_sets.add(record())
        uow.commit()
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, A) as tx:
        tx.session.execute(text("UPDATE chunking.chunk_sets SET chunk_count = 9"))
    assert sqlstate(caught.value) == "42501"  # no UPDATE grant
    with pytest.raises(DBAPIError) as owner:  # and not even the owner may
        admin(admin_engine, "UPDATE chunking.chunk_sets SET chunk_count = 9")
    assert sqlstate(owner.value) == "55000"


def test_receipts_are_insert_only_for_every_role(
    sql_factory: SqlChunkingUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:
    with sql_factory.begin(SYSTEM_A) as uow:
        uow.receipts.add(PurgeReceipt(A, D1, NOW, (V1,)))
        uow.commit()
    for statement in (
        "UPDATE chunking.purge_receipts SET versions_purged = 5",
        "DELETE FROM chunking.purge_receipts",
        "TRUNCATE chunking.purge_receipts",
        "TRUNCATE chunking.chunk_sets",
    ):
        with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, A) as tx:
            tx.session.execute(text(statement))
        assert sqlstate(caught.value) == "42501", statement
    for statement in (
        "UPDATE chunking.purge_receipts SET versions_purged = 5",
        "DELETE FROM chunking.purge_receipts",
        "TRUNCATE chunking.purge_receipts",
    ):
        with pytest.raises(DBAPIError) as owner:
            admin(admin_engine, statement)
        assert sqlstate(owner.value) == "55000", statement


@pytest.mark.parametrize(
    "bad",
    [
        {"h": "A" * 64},
        {"h": "a" * 63},
        {"eh": "x"},
        {"cv": ""},
        {"cv": "bad\nversion"},
        {"ab": 0},
        {"cc": 0},
        {"ch": 0},
    ],
)
def test_check_constraints_reject_bad_rows(admin_engine: Engine, bad: dict[str, object]) -> None:
    with pytest.raises(DBAPIError) as caught:
        admin(admin_engine, GOOD_ROW, **row(**bad))
    assert sqlstate(caught.value) == "23514"
    admin(admin_engine, GOOD_ROW, **row())  # the unmodified row is valid
    with pytest.raises(DBAPIError) as dup:
        admin(admin_engine, GOOD_ROW, **row())
    assert sqlstate(dup.value) == "23505"  # one chunk set per version


def test_receipt_scope_is_constrained(admin_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as caught:
        admin(
            admin_engine,
            "INSERT INTO chunking.purge_receipts VALUES (:t, :d, now(), 0, '[]', 'everything')",
            t=str(A.value),
            d=str(D1.value),
        )
    assert sqlstate(caught.value) == "23514"


# ---- the lifecycle lock ---------------------------------------------------------------------


def test_the_lock_serializes_one_document_and_never_blocks_another(
    sql_factory: SqlChunkingUnitOfWorkFactory,
) -> None:
    acquired = threading.Event()

    def contender(document_id: object) -> None:
        with sql_factory.begin(SYSTEM_A) as uow:
            uow.chunk_sets.lock_document(document_id)  # type: ignore[arg-type]
            acquired.set()

    holder = sql_factory.begin(SYSTEM_A)
    with holder as uow:
        uow.chunk_sets.lock_document(D1)
        other = threading.Thread(target=contender, args=(D1,))
        other.start()
        assert not acquired.wait(timeout=0.7)  # blocked while the holder's transaction is open
        free = threading.Thread(target=contender, args=(type(D1)(UUID(int=0xD9)),))
        free.start()
        free.join(timeout=10)
        assert not free.is_alive()  # a different document is never blocked
    other.join(timeout=10)  # the holder rolled back: released
    assert acquired.is_set() and not other.is_alive()


# ---- the stage and the purge on the real adapters -------------------------------------------


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


def sql_stage(rig: Rig, sql_factory: SqlChunkingUnitOfWorkFactory) -> ChunkStage:
    return ChunkStage(rig.reader, PARAMS, sql_factory, rig.objects, rig.clock)


BODY = "# Indemnity\n\nIndemnity clause.\n\nTermination clause."


def test_stage_on_postgres_records_once_and_is_idempotent(
    rig: Rig, sql_factory: SqlChunkingUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 3)
    stage = sql_stage(rig, sql_factory)
    document, version = rig.upload_document(BODY)
    request = rig.request(document, version, token=3)
    assert stage.run(request) == StageSucceeded()
    assert stage.run(request) == StageSucceeded()
    assert count(admin_engine, "chunk_sets") == 1
    with sql_factory.begin(SYSTEM_A) as uow:
        stored = uow.chunk_sets.get(VersionId(version.value))
    assert stored is not None and stored.chunk_count >= 1
    assert rig.objects.exists(rig.key(document, version))


def test_stage_on_postgres_is_fenced(
    rig: Rig, sql_factory: SqlChunkingUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 5)
    stage = sql_stage(rig, sql_factory)
    document, version = rig.upload_document(BODY)
    with pytest.raises(ConflictError):  # a stale executor holds token 4; the job is at 5
        stage.run(rig.request(document, version, token=4))
    assert count(admin_engine, "chunk_sets") == 0
    assert rig.objects.derived_keys() == []


def test_concurrent_executors_leave_one_row_and_one_artifact(
    rig: Rig, sql_factory: SqlChunkingUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 1)
    stage = sql_stage(rig, sql_factory)
    document, version = rig.upload_document(BODY)
    outcomes: list[StageOutcome] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def go() -> None:
        try:
            barrier.wait(timeout=30)
            outcomes.append(stage.run(rig.request(document, version, token=1)))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert outcomes == [StageSucceeded()] * 4
    assert count(admin_engine, "chunk_sets") == 1
    assert len(rig.objects.derived_keys()) == 1


def test_purge_on_postgres(
    rig: Rig, sql_factory: SqlChunkingUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 1)
    stage = sql_stage(rig, sql_factory)
    document, version = rig.upload_document(BODY)
    assert stage.run(rig.request(document, version)) == StageSucceeded()
    purge = PurgeChunks(sql_factory, rig.objects, rig.clock)
    assert purge.execute(SYSTEM_A, PurgeCommand(document)).versions_purged == 1
    assert count(admin_engine, "chunk_sets") == 0
    assert count(admin_engine, "purge_receipts") == 1
    assert not rig.objects.exists(rig.key(document, version))
    assert purge.execute(SYSTEM_A, PurgeCommand(document)).purged is False
    assert count(admin_engine, "purge_receipts") == 1


def test_a_stage_waits_for_a_purge_in_flight_and_then_refuses(
    rig: Rig, sql_factory: SqlChunkingUnitOfWorkFactory, admin_engine: Engine
) -> None:
    """The purge holds the lifecycle lock in its final transaction: a stage that arrives meanwhile
    cannot record. Once the receipt commits, it sees it and writes nothing."""
    seed_job(admin_engine, 0x7001, 1)
    stage = sql_stage(rig, sql_factory)
    document, version = rig.upload_document(BODY)
    outcomes: list[StageOutcome] = []
    started = threading.Event()

    def go() -> None:
        started.set()
        outcomes.append(stage.run(rig.request(document, version)))

    with sql_factory.begin(SYSTEM_A) as purge_tx:
        purge_tx.chunk_sets.lock_document(document)
        worker = threading.Thread(target=go)
        worker.start()
        started.wait(timeout=10)
        worker.join(timeout=0.7)
        assert worker.is_alive()  # blocked on the lock, nothing recorded yet
        purge_tx.receipts.add(PurgeReceipt(TENANT_A, document, NOW, ()))
        purge_tx.commit()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert outcomes == [StagePermanentFailure("version_unavailable")]
    assert count(admin_engine, "chunk_sets") == 0
    assert rig.objects.derived_keys() == []
