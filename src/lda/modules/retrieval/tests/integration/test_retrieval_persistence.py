"""Real Postgres + pgvector: schema ownership, forced RLS, least privilege, immutability, vector
shape, no text storage, the fence, the lifecycle lock and the stage/purge race."""

from __future__ import annotations

import threading
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.ingestion.api import StagePermanentFailure, StageSucceeded
from lda.modules.retrieval.api import IndexStage
from lda.modules.retrieval.domain.embedding import to_literal
from lda.modules.retrieval.domain.failures import VERSION_UNAVAILABLE
from lda.modules.retrieval.domain.record import PurgeReceipt
from lda.modules.retrieval.infrastructure.persistence.unit_of_work import (
    SqlRetrievalUnitOfWorkFactory,
)
from lda.modules.retrieval.tests.contract.test_retrieval_repository_contract import (
    D1,
    NOW,
    V1,
    A,
    B,
    commit,
    system,
    vector,
)
from lda.modules.retrieval.tests.support import SYSTEM_A, TENANT_A, build_rig
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import ConflictError

HASH = "a" * 64
TABLES = ("index_versions", "chunk_vectors", "purge_receipts")
ROLE = "lda_app_retrieval"
EXPECTED_COLUMNS = {
    "index_versions": {
        "tenant_id", "version_id", "index_id", "document_id", "embedding_provider",
        "embedding_model", "embedding_version", "dimensions", "chunker_version", "source_hash",
        "chunk_count", "created_at",
    },
    "chunk_vectors": {
        "tenant_id", "index_id", "chunk_id", "document_id", "version_id", "ordinal", "text_hash",
        "embedding",
    },
    "purge_receipts": {
        "tenant_id", "document_id", "purged_at", "versions_purged", "version_ids", "scope",
    },
}  # fmt: skip


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def admin(engine: Engine, statement: str, **params: object) -> None:
    with engine.begin() as conn:
        conn.execute(text(statement), params)


def test_the_schema_holds_exactly_the_approved_columns_and_no_text(admin_engine: Engine) -> None:
    """ADR-038/ADR-004: identifiers, hashes, identity, counts and vectors: never chunk text."""
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'retrieval'"
            )
        ).all()
    found: dict[str, set[str]] = {}
    for table, column in rows:
        found.setdefault(table, set()).add(column)
    assert found == EXPECTED_COLUMNS


def test_tables_live_only_in_the_schema_and_have_no_foreign_key_out(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'retrieval' AND table_type = 'BASE TABLE'"
            )
        ).scalars()
        fks = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace "
                "WHERE n.nspname = 'retrieval' AND c.contype = 'f'"
            )
        ).scalar_one()
    assert set(tables) == set(TABLES) and fks == 0


def test_the_embedding_column_is_a_256_dimension_pgvector(
    admin_engine: Engine, sql_factory: SqlRetrievalUnitOfWorkFactory
) -> None:
    commit(sql_factory)
    with admin_engine.connect() as conn:
        typ = conn.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'retrieval.chunk_vectors'::regclass AND attname = 'embedding'"
            )
        ).scalar_one()
        dims = (
            conn.execute(
                text("SELECT DISTINCT vector_dims(embedding) FROM retrieval.chunk_vectors")
            )
            .scalars()
            .all()
        )
    assert typ == "vector(256)" and dims == [256]
    with pytest.raises(DBAPIError):  # another size is another migration, never an insert
        admin(
            admin_engine,
            "INSERT INTO retrieval.chunk_vectors (tenant_id, index_id, chunk_id, document_id, "
            "version_id, ordinal, text_hash, embedding) VALUES (:t, :i, :c, :d, :v, 0, :h, "
            "CAST('[1,0,0]' AS public.vector))",
            t=str(A.value), i="0" * 32, c=str(UUID(int=9)), d=str(D1.value), v=str(V1.value),
            h="a" * 64,
        )  # fmt: skip


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
                {"t": f"retrieval.{table}"},
            ).one()
            policy = conn.execute(
                text(
                    "SELECT qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'retrieval' AND tablename = :t"
                ),
                {"t": table},
            ).all()
            leading = conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                    "ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
                    "WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary"
                ),
                {"t": f"retrieval.{table}"},
            ).scalar_one()
            assert tuple(flags) == (True, True), table
            assert len(policy) == 1 and all("app.tenant_id" in (p or "") for p in policy[0]), table
            assert leading == "tenant_id", table


def test_the_runtime_role_holds_exactly_the_approved_privileges(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'retrieval' AND grantee = :r"
            ),
            {"r": ROLE},
        ).all()
    granted: dict[str, set[str]] = {}
    for table, privilege in rows:
        granted.setdefault(table, set()).add(privilege)
    assert granted == {
        "index_versions": {"SELECT", "INSERT", "DELETE"},  # never UPDATE: immutable
        "chunk_vectors": {"SELECT", "INSERT", "DELETE"},
        "purge_receipts": {"SELECT", "INSERT"},
    }


def test_the_database_itself_enforces_tenant_scope(
    sql_factory: SqlRetrievalUnitOfWorkFactory, app_engine: Engine
) -> None:
    commit(sql_factory)
    with TenantTransaction(app_engine, B) as tx:
        for table in TABLES:
            query = f"SELECT count(*) FROM retrieval.{table}"  # noqa: S608
            assert tx.session.execute(text(query)).scalar_one() == 0
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, B) as tx:
        tx.session.execute(
            text(
                "INSERT INTO retrieval.index_versions (tenant_id, version_id, index_id, "
                "document_id, embedding_provider, embedding_model, embedding_version, dimensions, "
                "chunker_version, source_hash, chunk_count, created_at) VALUES (:t, :v, :i, :d, "
                "'p', 'm', '1', 256, 'c', :h, 1, now())"
            ),
            {
                "t": str(A.value),
                "v": str(V1.value),
                "i": "0" * 32,
                "d": str(D1.value),
                "h": "a" * 64,
            },
        )  # tenant A's row under tenant B's context
    assert sqlstate(caught.value) == "42501"
    with app_engine.connect() as conn:  # no tenant context at all: nothing is visible
        assert conn.execute(text("SELECT count(*) FROM retrieval.chunk_vectors")).scalar_one() == 0


def test_a_search_statement_under_another_tenant_context_finds_nothing(
    sql_factory: SqlRetrievalUnitOfWorkFactory, app_engine: Engine
) -> None:
    """The tenant predicate and RLS are independent: even a raw search under B sees no A vectors."""
    commit(sql_factory)
    with TenantTransaction(app_engine, B) as tx:
        rows = tx.session.execute(
            text(
                "SELECT chunk_id FROM retrieval.chunk_vectors "
                "ORDER BY embedding OPERATOR(public.<=>) CAST(:q AS public.vector) LIMIT 10"
            ),
            {"q": to_literal(vector("payment"))},
        ).all()
    assert rows == []


def test_index_rows_are_immutable_for_every_role(
    sql_factory: SqlRetrievalUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:
    commit(sql_factory)
    for statement in (
        "UPDATE retrieval.index_versions SET chunk_count = 9",
        "UPDATE retrieval.chunk_vectors SET ordinal = 9",
    ):
        with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, A) as tx:
            tx.session.execute(text(statement))
        assert sqlstate(caught.value) == "42501"  # no UPDATE grant
        with pytest.raises(DBAPIError) as owner:  # and not even the owner may
            admin(admin_engine, statement)
        assert sqlstate(owner.value) == "55000"


def test_receipts_are_insert_only_for_every_role(
    sql_factory: SqlRetrievalUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:
    with sql_factory.begin(system()) as uow:
        uow.receipts.add(PurgeReceipt(A, D1, NOW, (V1,)))
        uow.commit()
    for statement in (
        "UPDATE retrieval.purge_receipts SET versions_purged = 5",
        "DELETE FROM retrieval.purge_receipts",
        "TRUNCATE retrieval.purge_receipts",
        "TRUNCATE retrieval.chunk_vectors",
    ):
        with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, A) as tx:
            tx.session.execute(text(statement))
        assert sqlstate(caught.value) == "42501", statement
    for statement in (
        "UPDATE retrieval.purge_receipts SET versions_purged = 5",
        "DELETE FROM retrieval.purge_receipts",
        "TRUNCATE retrieval.purge_receipts",
    ):
        with pytest.raises(DBAPIError) as owner:
            admin(admin_engine, statement)
        assert sqlstate(owner.value) == "55000", statement


def test_the_receipt_scope_and_index_identity_are_constrained(admin_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as caught:
        admin(
            admin_engine,
            "INSERT INTO retrieval.purge_receipts VALUES (:t, :d, now(), 0, '[]', 'everything')",
            t=str(A.value),
            d=str(D1.value),
        )
    assert sqlstate(caught.value) == "23514"


# ---- the lifecycle lock -----------------------------------------------------------------------


def test_the_lock_serializes_one_document_and_never_blocks_another(
    sql_factory: SqlRetrievalUnitOfWorkFactory,
) -> None:
    acquired = threading.Event()

    def contender(document_id: object) -> None:
        with sql_factory.begin(SYSTEM_A) as uow:
            uow.index.lock_document(document_id)  # type: ignore[arg-type]
            acquired.set()

    with sql_factory.begin(SYSTEM_A) as uow:
        uow.index.lock_document(D1)
        other = threading.Thread(target=contender, args=(D1,))
        other.start()
        assert not acquired.wait(timeout=0.7)  # blocked while the holder's transaction is open
        free = threading.Thread(target=contender, args=(type(D1)(UUID(int=0xD9)),))
        free.start()
        free.join(timeout=10)
        assert not free.is_alive()  # a different document is never blocked
    other.join(timeout=10)
    assert acquired.is_set() and not other.is_alive()


# ---- the stage, search and purge on the real adapters -----------------------------------------

TEXTS = ["Payment is due monthly in advance", "Governing law is New York"]


def seed_job(admin_engine: Engine, job: int, token: int) -> None:
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


def test_stage_search_and_purge_on_postgres(
    sql_factory: SqlRetrievalUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 3)
    rig = build_rig(retrieval=sql_factory)
    document, version = rig.new_document(TEXTS)
    request = rig.request(document, version, token=3)
    assert rig.stage.run(request) == StageSucceeded()
    assert rig.stage.run(request) == StageSucceeded()  # duplicate delivery
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM retrieval.chunk_vectors")).scalar_one() == 2
    assert rig.search("payment due", [document]).hits == ()  # indexed but not ready yet
    rig.ready(document, version)
    hits = rig.search("payment due monthly", [document]).hits
    assert [h.ordinal for h in hits] == [0, 1] and hits[0].version_id == version
    assert rig.purge_document(document).purged
    assert rig.search("payment due", [document]).hits == ()
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM retrieval.chunk_vectors")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM retrieval.purge_receipts")).scalar_one() == 1


def test_stage_on_postgres_is_fenced(
    sql_factory: SqlRetrievalUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 5)
    rig = build_rig(retrieval=sql_factory)
    document, version = rig.new_document(TEXTS)
    with pytest.raises(ConflictError):  # a stale executor holds token 4; the job is at 5
        rig.stage.run(rig.request(document, version, token=4))
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM retrieval.chunk_vectors")).scalar_one() == 0


def test_a_stage_waits_for_a_purge_in_flight_and_then_refuses(
    sql_factory: SqlRetrievalUnitOfWorkFactory, admin_engine: Engine
) -> None:
    """The purge holds the lifecycle lock in its transaction: a stage that arrives meanwhile
    blocks on the lock, then sees the receipt and refuses, so no row outlives the purge."""
    seed_job(admin_engine, 0x7001, 3)
    rig = build_rig(retrieval=sql_factory)
    document, version = rig.new_document(TEXTS)
    outcome: list[object] = []
    stage: IndexStage = rig.stage
    request = rig.request(document, version, token=3)
    with sql_factory.begin(SYSTEM_A) as purge_tx:
        purge_tx.index.lock_document(document)
        worker = threading.Thread(target=lambda: outcome.append(stage.run(request)))
        worker.start()
        worker.join(timeout=1.0)
        assert worker.is_alive()  # blocked on the lock, nothing recorded yet
        purge_tx.receipts.add(PurgeReceipt(TENANT_A, document, NOW, ()))
        purge_tx.commit()
    worker.join(timeout=10)
    assert outcome == [StagePermanentFailure(VERSION_UNAVAILABLE)]
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM retrieval.chunk_vectors")).scalar_one() == 0


def test_concurrent_executors_leave_one_record_and_one_set_of_vectors(
    sql_factory: SqlRetrievalUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 3)
    rig = build_rig(retrieval=sql_factory)
    document, version = rig.new_document(TEXTS)
    results: list[object] = []

    def run() -> None:
        results.append(rig.stage.run(rig.request(document, version, token=3)))

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert results == [StageSucceeded()] * 4
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM retrieval.index_versions")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM retrieval.chunk_vectors")).scalar_one() == 2


def test_two_versions_are_both_recorded_but_only_the_newest_ready_is_searched(
    sql_factory: SqlRetrievalUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 3)
    rig = build_rig(retrieval=sql_factory)
    document, v1 = rig.new_document(["Payment terms version one"])
    rig.stage.run(rig.request(document, v1, token=3))
    rig.ready(document, v1)
    v2 = rig.new_version(document, ["Payment terms version two"])
    rig.stage.run(rig.request(document, v2, token=3))
    rig.ready(document, v2)
    hits = rig.search("payment terms", [document]).hits
    assert {h.version_id for h in hits} == {v2} and hits[0].version_number == 2
    with sql_factory.begin(system()) as uow:  # both versions still recorded
        assert len(uow.index.list_for(document)) == 2
