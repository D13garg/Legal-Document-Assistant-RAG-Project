"""Real Postgres: schema ownership, forced RLS, least privilege and the guard triggers."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.documents.domain.document import DocumentStatus
from lda.modules.documents.infrastructure.persistence.unit_of_work import (
    SqlDocumentsUnitOfWorkFactory,
)
from lda.modules.documents.tests.contract.test_repository_contract import (
    D1,
    V1,
    V2,
    make,
    seed,
    tombstone,
)
from lda.modules.documents.tests.support import START, TENANT_A, TENANT_B, ctx_for
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock

TABLES = ("documents", "versions", "upload_intents", "purge_receipts")


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def admin(engine: Engine, statement: str, **params: object) -> None:
    with engine.begin() as conn:
        conn.execute(text(statement), params)


def scalar(engine: Engine, statement: str) -> object:
    with engine.connect() as conn:
        return conn.execute(text(statement)).scalar_one()


def test_tables_live_only_in_the_documents_schema_and_have_no_foreign_key_out(
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
        }
        foreign = conn.execute(
            text(
                "SELECT t.relname, r.relname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_class r ON r.oid = c.confrelid "
                "JOIN pg_namespace s ON s.oid = r.relnamespace "
                "WHERE c.contype = 'f' AND t.relnamespace = 'documents'::regnamespace "
                "AND s.nspname <> 'documents'"
            )
        ).all()
    assert schemas == dict.fromkeys(TABLES, "documents")
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
                {"t": f"documents.{table}"},
            ).one()
            policy = conn.execute(
                text(
                    "SELECT qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'documents' AND tablename = :t"
                ),
                {"t": table},
            ).all()
            leading = conn.execute(
                text(
                    "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                    "ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
                    "WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary"
                ),
                {"t": f"documents.{table}"},
            ).scalar_one()
            assert tuple(flags) == (True, True), table
            assert len(policy) == 1 and all("app.tenant_id" in (p or "") for p in policy[0]), table
            assert leading == "tenant_id", table


def test_the_runtime_role_holds_exactly_the_approved_privileges(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE table_schema = 'documents' AND grantee = 'lda_app_documents'"
            )
        ).all()
    granted: dict[str, set[str]] = {}
    for table, privilege in rows:
        granted.setdefault(table, set()).add(privilege)
    assert granted == {
        "documents": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "versions": {"SELECT", "INSERT", "DELETE"},  # never UPDATE in step 5 (step 6 adds status)
        "upload_intents": {"SELECT", "INSERT", "UPDATE", "DELETE"},  # UPDATE only to lock
        "purge_receipts": {"SELECT", "INSERT"},
    }


def test_the_runtime_role_cannot_update_versions_or_receipts_or_truncate(
    sql_factory: SqlDocumentsUnitOfWorkFactory, app_engine: Engine
) -> None:
    seed(sql_factory)
    for statement in (
        "UPDATE documents.versions SET size_bytes = 1",
        "UPDATE documents.purge_receipts SET scope = scope",
        "TRUNCATE documents.documents",
        "TRUNCATE documents.purge_receipts",
    ):
        with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, TENANT_A) as tx:
            tx.session.execute(text(statement))
        assert sqlstate(caught.value) == "42501", statement


def test_the_database_itself_enforces_tenant_scope(
    sql_factory: SqlDocumentsUnitOfWorkFactory, app_engine: Engine
) -> None:
    seed(sql_factory)
    with TenantTransaction(app_engine, TENANT_B) as tx:
        assert (
            tx.session.execute(text("SELECT count(*) FROM documents.documents")).scalar_one() == 0
        )
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, TENANT_B) as tx:
        tx.session.execute(
            text("INSERT INTO documents.upload_intents VALUES (:t, :v, :d, now())"),
            {"t": str(TENANT_A), "v": str(V1), "d": str(D1)},
        )
    assert sqlstate(caught.value) == "42501"
    with app_engine.begin() as conn:  # no tenant scope: nothing is visible ...
        assert conn.execute(text("SELECT count(*) FROM documents.documents")).scalar_one() == 0
    with pytest.raises(DBAPIError) as unscoped, app_engine.begin() as conn:  # ... or writable
        conn.execute(
            text("INSERT INTO documents.upload_intents VALUES (:t, :v, :d, now())"),
            {"t": str(TENANT_A), "v": str(V1), "d": str(D1)},
        )
    assert sqlstate(unscoped.value) == "42501"


def test_the_document_guard_triggers_hold_even_for_the_owner(
    sql_factory: SqlDocumentsUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed(sql_factory)
    for statement in (
        "UPDATE documents.documents SET title = 'other'",
        "UPDATE documents.documents SET document_id = '00000000-0000-0000-0000-0000000000ff'",
        "UPDATE documents.documents SET tenant_id = '00000000-0000-0000-0000-0000000000ff'",
        "UPDATE documents.documents SET created_at = created_at + interval '1 day'",
        "UPDATE documents.documents SET created_by = NULL",
        "UPDATE documents.documents SET latest_version_number = 3",
        "UPDATE documents.documents SET latest_version_number = 0",
        "UPDATE documents.documents SET status = 'deleted', deleted_at = now(), "
        "latest_version_number = 2",
        "DELETE FROM documents.documents",
    ):
        with pytest.raises(DBAPIError) as caught:
            admin(admin_engine, statement)
        assert sqlstate(caught.value) in {"55000", "23514"}, statement
    admin(admin_engine, "UPDATE documents.documents SET latest_version_number = 2")  # +1 is allowed


def test_a_tombstoned_document_is_frozen_and_is_removed_only_after_its_versions(
    sql_factory: SqlDocumentsUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed(sql_factory)
    tombstone(sql_factory)
    for statement in (
        "UPDATE documents.documents SET latest_version_number = 2",
        "UPDATE documents.documents SET status = 'active', deleted_at = NULL",
    ):
        with pytest.raises(DBAPIError) as caught:
            admin(admin_engine, statement)
        assert sqlstate(caught.value) == "55000", statement
    with pytest.raises(DBAPIError) as caught:  # versions still reference it
        admin(admin_engine, "DELETE FROM documents.documents")
    assert sqlstate(caught.value) == "23503"


def test_versions_are_immutable_and_deletable_only_through_a_tombstoned_document(
    sql_factory: SqlDocumentsUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed(sql_factory)
    for column, value in (
        ("tenant_id", "'00000000-0000-0000-0000-0000000000ff'"),
        ("version_id", "'00000000-0000-0000-0000-0000000000ff'"),
        ("document_id", "'00000000-0000-0000-0000-0000000000ff'"),
        ("size_bytes", "99"),
        ("media_type", "'text/plain'"),
        ("number", "7"),
        ("created_at", "created_at + interval '1 day'"),
        ("created_by", "NULL"),
    ):
        with pytest.raises(DBAPIError) as caught:
            admin(admin_engine, f"UPDATE documents.versions SET {column} = {value}")  # noqa: S608
        assert sqlstate(caught.value) == "55000", column
    with pytest.raises(DBAPIError) as caught:
        admin(admin_engine, "DELETE FROM documents.versions")
    assert sqlstate(caught.value) == "55000"
    tombstone(sql_factory)
    admin(admin_engine, "DELETE FROM documents.versions")
    assert scalar(admin_engine, "SELECT count(*) FROM documents.versions") == 0


def test_only_the_four_lifecycle_statuses_are_admitted(
    sql_factory: SqlDocumentsUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed(sql_factory)
    for status in ("PENDING", "done", "archived", ""):
        with pytest.raises(DBAPIError) as caught:
            admin(admin_engine, "UPDATE documents.versions SET status = :s", s=status)
        assert sqlstate(caught.value) == "23514"


def test_an_upload_intent_can_be_locked_but_never_changed(
    sql_factory: SqlDocumentsUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:
    admin(
        admin_engine,
        "INSERT INTO documents.upload_intents VALUES (:t, :v, :d, now())",
        t=str(TENANT_A),
        v=str(V1),
        d=str(D1),
    )
    with TenantTransaction(app_engine, TENANT_A) as tx:  # FOR UPDATE works for the runtime role
        locked = tx.session.execute(
            text("SELECT version_id FROM documents.upload_intents FOR UPDATE")
        ).all()
        assert len(locked) == 1
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(text("UPDATE documents.upload_intents SET created_at = now()"))
    assert sqlstate(caught.value) == "55000"


def test_purge_receipts_are_insert_only_for_every_role_and_truncate_is_blocked(
    sql_factory: SqlDocumentsUnitOfWorkFactory, admin_engine: Engine
) -> None:
    admin(
        admin_engine,
        "INSERT INTO documents.purge_receipts VALUES (:t, :d, now(), now(), 0, '[]', "
        "'documents.originals_and_rows')",
        t=str(TENANT_A),
        d=str(D1),
    )
    for statement in (
        "UPDATE documents.purge_receipts SET versions_purged = 1",
        "DELETE FROM documents.purge_receipts",
        "TRUNCATE documents.purge_receipts",
    ):
        with pytest.raises(DBAPIError) as caught:
            admin(admin_engine, statement)
        assert sqlstate(caught.value) == "55000", statement
    assert scalar(admin_engine, "SELECT count(*) FROM documents.purge_receipts") == 1


def test_check_constraints_mirror_the_domain(admin_engine: Engine) -> None:
    base = "INSERT INTO documents.documents VALUES (:t, :d, :title, :s, NULL, now(), :n, :del)"
    good = {"t": str(TENANT_A), "d": str(D1), "title": "ok", "s": "active", "n": 1, "del": None}
    for over in (
        {"title": ""},
        {"title": "x" * 256},
        {"s": "archived"},
        {"n": 0},
        {"s": "deleted"},  # deleted needs deleted_at
        {"del": START},  # active must not have deleted_at
    ):
        with pytest.raises(DBAPIError) as caught:
            admin(admin_engine, base, **{**good, **over})
        assert sqlstate(caught.value) == "23514", over
    admin(admin_engine, base, **good)


def test_a_failed_statement_leaks_neither_titles_nor_parameters(
    sql_factory: SqlDocumentsUnitOfWorkFactory,
) -> None:
    from lda.shared.kernel.errors import InfrastructureError

    document, version = make()
    private_title = "Privileged merger memo"
    with sql_factory.begin(ctx_for(TENANT_A)) as uow:
        uow.documents.add(document)
        uow.versions.add(version)
        uow.commit()
    clash, _ = make()
    clash.title = private_title
    with sql_factory.begin(ctx_for(TENANT_A)) as uow, pytest.raises(InfrastructureError) as caught:
        uow.documents.add(clash)  # duplicate key: the driver message would quote the row
    assert caught.value.code == "documents_persistence_failed"
    assert private_title not in str(caught.value) + caught.value.detail
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_unused_names_stay_referenced() -> None:
    assert (DocumentStatus.ACTIVE, V2, FakeClock(START), timedelta(0)) is not None


@pytest.mark.usefixtures("admin_engine")
def test_for_update_really_locks_so_a_reaper_and_an_upload_cannot_both_win(
    sql_factory: SqlDocumentsUnitOfWorkFactory,
) -> None:
    """The reaper's decision and the upload's own commit serialize on the intent row; versions,
    tombstone and purge serialize on the document row (ADR-005, ADR-015, ADR-023)."""
    from lda.modules.documents.domain.evidence import UploadIntent
    from lda.shared.kernel.errors import InfrastructureError

    seed(sql_factory)
    with sql_factory.begin(ctx_for(TENANT_A)) as uow:
        uow.intents.add(UploadIntent(TENANT_A, V2, D1, START))
        uow.commit()
    with sql_factory.begin(ctx_for(TENANT_A)) as holder:
        assert holder.documents.get(D1, for_update=True) is not None
        assert holder.intents.get(V2, for_update=True) is not None
        with sql_factory.begin(ctx_for(TENANT_A)) as rival:
            rival._inner.transaction.session.execute(text("SET LOCAL lock_timeout = '200ms'"))  # noqa: SLF001
            with pytest.raises(InfrastructureError):
                rival.documents.get(D1, for_update=True)
        with sql_factory.begin(ctx_for(TENANT_A)) as rival:
            rival._inner.transaction.session.execute(text("SET LOCAL lock_timeout = '200ms'"))  # noqa: SLF001
            with pytest.raises(InfrastructureError):
                rival.intents.get(V2, for_update=True)
        with sql_factory.begin(ctx_for(TENANT_A)) as reader:  # plain reads are never blocked
            assert reader.documents.get(D1) is not None and reader.intents.get(V2) is not None
    with sql_factory.begin(ctx_for(TENANT_A)) as free:  # the locks are released with the unit
        assert free.documents.get(D1, for_update=True) is not None
        assert free.intents.get(V2, for_update=True) is not None


def test_version_check_constraints_mirror_the_domain(admin_engine: Engine) -> None:
    admin(
        admin_engine,
        "INSERT INTO documents.documents VALUES (:t, :d, 'x', 'active', NULL, now(), 1, NULL)",
        t=str(TENANT_A),
        d=str(D1),
    )
    base = "INSERT INTO documents.versions VALUES (:t, :v, :d, :n, 'pending', :m, :s, NULL, now())"
    good = {"t": str(TENANT_A), "v": str(V1), "d": str(D1), "n": 1, "m": "application/pdf", "s": 5}
    for over in (
        {"s": 0},
        {"n": 0},
        {"m": "APPLICATION/PDF"},
        {"m": "pdf"},
        {"m": "a" * 64 + "/b"},
    ):
        with pytest.raises(DBAPIError) as caught:
            admin(admin_engine, base, **{**good, **over})
        assert sqlstate(caught.value) == "23514", over
    admin(admin_engine, base, **good)


def test_a_receipt_can_only_claim_the_documents_scope(admin_engine: Engine) -> None:
    base = "INSERT INTO documents.purge_receipts VALUES (:t, :d, now(), now(), 0, :ids, :scope)"
    good = {
        "t": str(TENANT_A),
        "d": str(D1),
        "ids": "[]",
        "scope": "documents.originals_and_rows",
    }
    for over in ({"scope": "documents.everything"}, {"scope": ""}, {"ids": '{"a": 1}'}):
        with pytest.raises(DBAPIError) as caught:
            admin(admin_engine, base, **{**good, **over})
        assert sqlstate(caught.value) == "23514", over
    admin(admin_engine, base, **good)
