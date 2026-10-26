"""Real Postgres: schema ownership, forced RLS, least privilege, immutability and the fence."""

from __future__ import annotations

import threading
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.document_processing.api import ExtractStage, PurgeCommand, PurgeExtractions
from lda.modules.document_processing.domain.ids import VersionId
from lda.modules.document_processing.domain.record import ExtractionRecord, PurgeReceipt
from lda.modules.document_processing.infrastructure.parsers.registry import FormatParsers
from lda.modules.document_processing.infrastructure.persistence.unit_of_work import (
    SqlProcessingUnitOfWorkFactory,
)
from lda.modules.document_processing.tests.contract.test_repository_contract import (
    D1,
    NOW,
    V1,
    A,
    B,
    record,
)
from lda.modules.document_processing.tests.support import SYSTEM_A, TENANT_A, Rig
from lda.modules.ingestion.api import StageOutcome, StageSucceeded
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import ConflictError

TABLES = ("extractions", "purge_receipts")
ROLE = "lda_app_document_processing"
HASH = "a" * 64
GOOD_ROW = (
    "INSERT INTO document_processing.extractions (tenant_id, version_id, document_id, "
    "parser_version, content_hash, media_type, artifact_bytes, block_count, character_count, "
    "page_count, created_at) VALUES (:t, :v, :d, :pv, :h, :mt, :ab, :bc, :cc, :pc, now())"
)


def row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "t": str(A.value),
        "v": str(V1.value),
        "d": str(D1.value),
        "pv": "extract-1",
        "h": HASH,
        "mt": "application/pdf",
        "ab": 10,
        "bc": 1,
        "cc": 5,
        "pc": None,
    }
    return base | over


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def admin(engine: Engine, statement: str, **params: object) -> None:
    with engine.begin() as conn:
        conn.execute(text(statement), params)


def count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        query = f"SELECT count(*) FROM document_processing.{table}"  # noqa: S608
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
                "WHERE c.contype = 'f' AND t.relnamespace = 'document_processing'::regnamespace"
            )
        ).all()
    assert schemas == dict.fromkeys(TABLES, "document_processing")
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
                {"t": f"document_processing.{table}"},
            ).one()
            policy = conn.execute(
                text(
                    "SELECT qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'document_processing' AND tablename = :t"
                ),
                {"t": table},
            ).all()
            leading = conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                    "ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
                    "WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary"
                ),
                {"t": f"document_processing.{table}"},
            ).scalar_one()
            assert tuple(flags) == (True, True), table
            assert len(policy) == 1 and all("app.tenant_id" in (p or "") for p in policy[0]), table
            assert leading == "tenant_id", table


def test_the_runtime_role_holds_exactly_the_approved_privileges(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'document_processing' AND grantee = :r"
            ),
            {"r": ROLE},
        ).all()
    granted: dict[str, set[str]] = {}
    for table, privilege in rows:
        granted.setdefault(table, set()).add(privilege)
    assert granted == {
        "extractions": {"SELECT", "INSERT", "DELETE"},  # never UPDATE: immutable
        "purge_receipts": {"SELECT", "INSERT"},
    }


def test_the_database_itself_enforces_tenant_scope(
    sql_factory: SqlProcessingUnitOfWorkFactory, app_engine: Engine
) -> None:
    with sql_factory.begin(SYSTEM_A) as uow:
        uow.extractions.add(_record())
        uow.commit()
    with TenantTransaction(app_engine, B) as tx:
        for table in TABLES:
            assert (
                tx.session.execute(
                    text(f"SELECT count(*) FROM document_processing.{table}")  # noqa: S608
                ).scalar_one()
                == 0
            )
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, B) as tx:
        tx.session.execute(text(GOOD_ROW), row())  # tenant A's row under tenant B's context
    assert sqlstate(caught.value) == "42501"
    with app_engine.connect() as conn:  # no tenant context at all: nothing is visible
        assert (
            conn.execute(text("SELECT count(*) FROM document_processing.extractions")).scalar_one()
            == 0
        )


def _record() -> ExtractionRecord:
    return record()


def test_extractions_are_immutable_for_every_role(
    sql_factory: SqlProcessingUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:
    with sql_factory.begin(SYSTEM_A) as uow:
        uow.extractions.add(_record())
        uow.commit()
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, A) as tx:
        tx.session.execute(text("UPDATE document_processing.extractions SET block_count = 9"))
    assert sqlstate(caught.value) == "42501"  # no UPDATE grant
    with pytest.raises(DBAPIError) as owner:  # and not even the owner may
        admin(admin_engine, "UPDATE document_processing.extractions SET block_count = 9")
    assert sqlstate(owner.value) == "55000"


def test_receipts_are_insert_only_for_every_role(
    sql_factory: SqlProcessingUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:

    with sql_factory.begin(SYSTEM_A) as uow:
        uow.receipts.add(PurgeReceipt(A, D1, NOW, (V1,)))
        uow.commit()
    for statement in (
        "UPDATE document_processing.purge_receipts SET versions_purged = 5",
        "DELETE FROM document_processing.purge_receipts",
        "TRUNCATE document_processing.purge_receipts",
        "TRUNCATE document_processing.extractions",
    ):
        with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, A) as tx:
            tx.session.execute(text(statement))
        assert sqlstate(caught.value) == "42501", statement
    for statement in (
        "UPDATE document_processing.purge_receipts SET versions_purged = 5",
        "DELETE FROM document_processing.purge_receipts",
        "TRUNCATE document_processing.purge_receipts",
    ):
        with pytest.raises(DBAPIError) as owner:
            admin(admin_engine, statement)
        assert sqlstate(owner.value) == "55000", statement


@pytest.mark.parametrize(
    "bad",
    [
        {"h": "A" * 64},
        {"h": "a" * 63},
        {"pv": ""},
        {"pv": "bad\nversion"},
        {"mt": "NOT A TYPE"},
        {"ab": 0},
        {"bc": 0},
        {"cc": 0},
        {"pc": 0},
    ],
)
def test_check_constraints_reject_bad_rows(admin_engine: Engine, bad: dict[str, object]) -> None:
    with pytest.raises(DBAPIError) as caught:
        admin(admin_engine, GOOD_ROW, **row(**bad))
    assert sqlstate(caught.value) == "23514"
    admin(admin_engine, GOOD_ROW, **row())  # the unmodified row is valid
    with pytest.raises(DBAPIError) as dup:
        admin(admin_engine, GOOD_ROW, **row())
    assert sqlstate(dup.value) == "23505"  # one extraction per version


def test_receipt_scope_is_constrained(admin_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as caught:
        admin(
            admin_engine,
            "INSERT INTO document_processing.purge_receipts VALUES "
            "(:t, :d, now(), 0, '[]', 'everything')",
            t=str(A.value),
            d=str(D1.value),
        )
    assert sqlstate(caught.value) == "23514"


# ---- the stage and the purge on the real adapters ---------------------------------------------


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


def sql_stage(rig: Rig, sql_factory: SqlProcessingUnitOfWorkFactory) -> ExtractStage:
    stage = ExtractStage(
        rig.reader, FormatParsers(), sql_factory, rig.objects, rig.clock, rig.limits
    )
    return stage


BODY = b"Indemnity clause.\n\nTermination clause."


def test_stage_on_postgres_records_once_and_is_idempotent(
    rig: Rig, sql_factory: SqlProcessingUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 3)
    stage = sql_stage(rig, sql_factory)
    document, version = rig.upload_document(BODY)
    request = rig.request(document, version, token=3)
    assert stage.run(request) == StageSucceeded()
    assert stage.run(request) == StageSucceeded()
    assert count(admin_engine, "extractions") == 1
    with sql_factory.begin(SYSTEM_A) as uow:
        stored = uow.extractions.get(VersionId(version.value))
    assert stored is not None and stored.block_count == 2
    assert rig.objects.exists(rig.key(document, version))


def test_stage_on_postgres_is_fenced(
    rig: Rig, sql_factory: SqlProcessingUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 5)
    stage = sql_stage(rig, sql_factory)
    document, version = rig.upload_document(BODY)
    with pytest.raises(ConflictError):  # a stale executor holds token 4; the job is at 5
        stage.run(rig.request(document, version, token=4))
    assert count(admin_engine, "extractions") == 0
    assert rig.objects.derived_keys() == []


def test_concurrent_executors_leave_one_row_and_one_artifact(
    rig: Rig, sql_factory: SqlProcessingUnitOfWorkFactory, admin_engine: Engine
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
    assert count(admin_engine, "extractions") == 1
    assert len(rig.objects.derived_keys()) == 1


def test_purge_on_postgres(
    rig: Rig, sql_factory: SqlProcessingUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed_job(admin_engine, 0x7001, 1)
    stage = sql_stage(rig, sql_factory)
    document, version = rig.upload_document(BODY)
    assert stage.run(rig.request(document, version)) == StageSucceeded()
    purge = PurgeExtractions(sql_factory, rig.objects, rig.clock)
    assert purge.execute(SYSTEM_A, PurgeCommand(document)).versions_purged == 1
    assert count(admin_engine, "extractions") == 0
    assert count(admin_engine, "purge_receipts") == 1
    assert not rig.objects.exists(rig.key(document, version))
    assert purge.execute(SYSTEM_A, PurgeCommand(document)).purged is False
    assert count(admin_engine, "purge_receipts") == 1
