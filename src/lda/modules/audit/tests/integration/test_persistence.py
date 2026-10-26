"""Real Postgres: schema ownership, insert-only, forced RLS, atomicity, error hygiene (ADR-019)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, OperationalError

from lda.modules.audit.domain.errors import InvalidAuditEntry
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.audit.infrastructure.persistence.tables import records
from lda.modules.audit.tests.conftest import ACTOR, REQUEST, TENANT_A, TENANT_B, a_fact
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.ports.jobs import JobSpec
from platform_support import make_wiring

RAW = (
    "INSERT INTO audit.records "
    "(tenant_id, action, resource_type, outcome, occurred_at, attributes) "
    "VALUES (:t, :a, :rt, :o, now(), CAST(:attrs AS jsonb))"
)


def raw_params(**over: object) -> dict[str, object]:
    return {"t": str(TENANT_A), "a": "x.did", "rt": "x", "o": "success", "attrs": "{}", **over}


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def count(admin: Engine) -> int:
    with admin.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM audit.records")).scalar_one())


# ---- ownership and structure -------------------------------------------------------------------


def test_the_table_lives_only_in_the_audit_schema(admin_engine: Engine) -> None:
    assert records.schema == "audit"
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = 'records' AND c.relkind = 'r'"
            )
        ).all()
    assert [r[0] for r in rows] == ["audit"]


def test_no_foreign_keys_exist(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace s ON s.oid = t.relnamespace "
                "WHERE s.nspname = 'audit' AND c.contype = 'f'"
            )
        ).scalar_one()
    assert n == 0


def test_rls_is_enabled_forced_and_the_key_leads_with_the_tenant(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        flags = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = 'audit.records'::regclass"
            )
        ).one()
        policies = conn.execute(
            text("SELECT qual, with_check FROM pg_policies WHERE schemaname='audit'")
        ).all()
        leading = conn.execute(
            text(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
                "ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
                "WHERE i.indrelid = 'audit.records'::regclass AND i.indisprimary"
            )
        ).scalar_one()
    assert tuple(flags) == (True, True)
    assert len(policies) == 1 and all("app.tenant_id" in (p or "") for p in policies[0])
    assert leading == "tenant_id"


def test_the_record_id_is_database_generated_and_cannot_be_supplied(
    admin_engine: Engine,
) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text(RAW), raw_params())
        conn.execute(text(RAW), raw_params())
        ids = [r[0] for r in conn.execute(text("SELECT record_id FROM audit.records ORDER BY 1"))]
    assert len(ids) == 2 and ids[0] < ids[1]
    with pytest.raises(DBAPIError) as caught, admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit.records (record_id, tenant_id, action, resource_type, outcome, "
                "occurred_at, attributes) VALUES (999, :t, 'x.did', 'x', 'success', now(), '{}')"
            ),
            {"t": str(TENANT_A)},
        )
    assert sqlstate(caught.value) == "428C9"  # GENERATED ALWAYS


# ---- insert-only (D7) --------------------------------------------------------------------------


def test_the_runtime_role_holds_insert_and_nothing_else(
    app_engine: Engine, admin_engine: Engine
) -> None:
    with admin_engine.connect() as conn:
        granted = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE table_schema = 'audit' AND table_name = 'records' "
                    "AND grantee = 'lda_app_audit'"
                )
            )
        }
    assert granted == {"INSERT"}


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM audit.records",
        "UPDATE audit.records SET action = 'x.y'",
        "DELETE FROM audit.records",
        "TRUNCATE audit.records",
    ],
)
def test_the_runtime_role_can_neither_read_change_delete_nor_truncate(
    app_engine: Engine, statement: str
) -> None:
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(text(statement))
    assert sqlstate(caught.value) == "42501"  # insufficient_privilege


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit.records SET action = 'x.changed'",
        "UPDATE audit.records SET tenant_id = tenant_id",
        "DELETE FROM audit.records",
        "TRUNCATE audit.records",
        "TRUNCATE audit.records CASCADE",
    ],
)
def test_even_the_owner_cannot_update_delete_or_truncate(
    admin_engine: Engine, statement: str
) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text(RAW), raw_params())
    with pytest.raises(DBAPIError) as caught, admin_engine.begin() as conn:
        conn.execute(text(statement))
    assert sqlstate(caught.value) == "55000"
    assert count(admin_engine) == 1


# ---- tenant isolation --------------------------------------------------------------------------


def test_a_runtime_write_for_another_tenant_is_rejected_by_rls(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as caught, TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(text(RAW), raw_params(t=str(TENANT_B)))
    assert sqlstate(caught.value) == "42501"  # new row violates row-level security policy


def test_without_a_tenant_scope_no_write_is_possible(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as caught, app_engine.begin() as conn:
        conn.execute(text(RAW), raw_params())
    assert sqlstate(caught.value) == "42501"
    with pytest.raises(DBAPIError), app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        conn.execute(text(RAW), raw_params())


def test_the_recorder_refuses_a_fact_for_another_tenant_before_any_sql(
    app_engine: Engine, admin_engine: Engine
) -> None:
    with TenantTransaction(app_engine, TENANT_A) as tx:
        with pytest.raises(InvalidAuditEntry) as caught:
            SqlAuditRecorder(tx).record(a_fact(tenant_id=TENANT_B))
        assert caught.value.code == "audit_tenant_mismatch"
        tx.commit()
    assert count(admin_engine) == 0


# ---- database constraints mirror the domain ----------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"a": "Bad Action"},
        {"a": "a." + "b" * 130},
        {"rt": "Bad"},
        {"rt": "x" * 65},
        {"o": "ok"},
        {"attrs": "[]"},
        {"attrs": '"text"'},
        {"attrs": "{" + ",".join(f'"k{i}": "v"' for i in range(17)) + "}"},
    ],
)
def test_the_table_itself_rejects_what_the_domain_would(
    admin_engine: Engine, over: dict[str, object]
) -> None:
    with pytest.raises(DBAPIError) as caught, admin_engine.begin() as conn:
        conn.execute(text(RAW), raw_params(**over))
    assert sqlstate(caught.value) == "23514"
    assert count(admin_engine) == 0


def test_the_table_accepts_sixteen_attributes(admin_engine: Engine) -> None:
    attrs = "{" + ",".join(f'"k{i}": "v"' for i in range(16)) + "}"
    with admin_engine.begin() as conn:
        conn.execute(text(RAW), raw_params(attrs=attrs))
    assert count(admin_engine) == 1


def test_resource_id_check_rejects_text_like_values(admin_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as caught, admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit.records (tenant_id, action, resource_type, resource_id, "
                "outcome, occurred_at, attributes) VALUES "
                "(:t, 'x.did', 'x', 'free text here', 'success', now(), '{}')"
            ),
            {"t": str(TENANT_A)},
        )
    assert sqlstate(caught.value) == "23514"


# ---- atomic with the action, fail-closed -------------------------------------------------------

SPEC = JobSpec(queue="q", job_type="t")


def jobs_count(admin: Engine) -> int:
    with admin.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM platform.jobs")).scalar_one())


def test_the_audit_row_commits_and_rolls_back_with_the_actions_own_transaction(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    factory = make_wiring(app_engine, clock, ids, SqlAuditRecorder).factory
    ctx = TenantContext(TENANT_A, ACTOR, REQUEST)
    with factory.begin(ctx) as uow:  # rolled back: neither the job nor the audit row survive
        uow.jobs.enqueue(SPEC)
        uow.audit.record(a_fact())
    assert (jobs_count(admin_engine), count(admin_engine)) == (0, 0)
    with factory.begin(ctx) as uow:
        uow.jobs.enqueue(SPEC)
        uow.audit.record(a_fact())
        uow.commit()
    assert (jobs_count(admin_engine), count(admin_engine)) == (1, 1)


def test_an_invalid_fact_aborts_the_action_and_its_other_effects(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    factory = make_wiring(app_engine, clock, ids, SqlAuditRecorder).factory
    ctx = TenantContext(TENANT_A, ACTOR, REQUEST)
    with pytest.raises(InvalidAuditEntry), factory.begin(ctx) as uow:
        uow.jobs.enqueue(SPEC)
        uow.audit.record(a_fact(attributes={"document_text": "secret clause"}))
        uow.commit()
    assert (jobs_count(admin_engine), count(admin_engine)) == (0, 0)


class _ExplodingSession:
    def execute(self, *args: object, **kwargs: object) -> None:
        raise OperationalError(
            "INSERT ... VALUES ('secret-value')",
            {"p": "secret-value"},
            Exception("boom secret-value"),
        )


class _ExplodingTx:
    tenant_id = TENANT_A
    session = _ExplodingSession()


def test_a_storage_failure_fails_closed_with_a_code_and_no_parameters() -> None:
    recorder = SqlAuditRecorder(_ExplodingTx())  # type: ignore[arg-type]
    with pytest.raises(InfrastructureError) as caught:
        recorder.record(a_fact(attributes={"k": "secret-value"}))
    assert caught.value.code == "audit_persistence_failed"
    assert "secret-value" not in str(caught.value) + caught.value.detail
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_dropped_connection_mid_unit_fails_the_action(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    factory = make_wiring(app_engine, clock, ids, SqlAuditRecorder).factory
    ctx = TenantContext(TENANT_A, ACTOR, REQUEST)
    with pytest.raises(InfrastructureError) as caught, factory.begin(ctx) as uow:
        with admin_engine.begin() as conn:  # kill this unit's backend connection
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE usename = 'lda_app_audit' AND pid <> pg_backend_pid()"
                )
            )
        uow.audit.record(a_fact())
        uow.commit()
    assert caught.value.code == "audit_persistence_failed"
    assert count(admin_engine) == 0


def test_occurred_at_keeps_its_instant(app_engine: Engine, admin_engine: Engine) -> None:
    with TenantTransaction(app_engine, TENANT_A) as tx:
        SqlAuditRecorder(tx).record(
            a_fact(occurred_at=datetime(2026, 3, 4, 5, 6, 7, 8, tzinfo=UTC))
        )
        tx.commit()
    with admin_engine.connect() as conn:
        got = conn.execute(text("SELECT occurred_at FROM audit.records")).scalar_one()
    assert got == datetime(2026, 3, 4, 5, 6, 7, 8, tzinfo=UTC)
