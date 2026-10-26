"""Tenant isolation proofs against a real Postgres (ADR-013) and structural schema rules (ADR-027)."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.platform.database.jobs.queue import PostgresJobQueue
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.ports.jobs import JobSpec
from platform_support import tenant

TENANT_TABLES = """
SELECT n.nspname AS schema, c.relname AS table, c.relrowsecurity AS rls, c.relforcerowsecurity AS forced
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
WHERE c.relkind = 'r' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
"""


def seed(engine: Engine, clock: FakeClock, ids: SequentialIdGenerator) -> None:
    for n in (1, 2):
        with TenantTransaction(engine, tenant(n)) as tx:
            PostgresJobQueue(tx, clock, ids).enqueue(JobSpec(queue="q", job_type=f"t{n}"))
            tx.commit()


def count_without_predicate(engine: Engine, scope: dict[str, str]) -> int:
    """The predicate-removal test: no WHERE tenant_id clause at all, RLS alone must isolate."""
    with engine.connect() as conn, conn.begin():
        for name, value in scope.items():
            conn.execute(text("SELECT set_config(:n, :v, true)"), {"n": name, "v": value})
        return int(conn.execute(text("SELECT count(*) FROM platform.jobs")).scalar_one())


def test_unfiltered_query_sees_only_own_tenant(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    seed(app_engine, clock, ids)
    assert count_without_predicate(app_engine, {"app.tenant_id": str(tenant(1))}) == 1
    assert count_without_predicate(app_engine, {"app.tenant_id": str(tenant(2))}) == 1


def test_unset_or_empty_scope_fails_closed(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    seed(app_engine, clock, ids)
    assert count_without_predicate(app_engine, {}) == 0
    assert count_without_predicate(app_engine, {"app.tenant_id": ""}) == 0


def test_cannot_write_rows_for_another_tenant(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, tenant(1)) as tx:
        tx.session.execute(
            text(
                "INSERT INTO platform.jobs (id, tenant_id, queue, job_type, payload, status, attempts, "
                "max_attempts, run_at, created_at, updated_at) VALUES (gen_random_uuid(), :t, 'q', 'x', "
                "'{}', 'queued', 0, 1, now(), now(), now())"
            ),
            {"t": str(tenant(2))},
        )


def test_runtime_role_is_not_privileged(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
    assert (row.rolsuper, row.rolbypassrls) == (False, False)


def test_transaction_is_unusable_after_commit(app_engine: Engine) -> None:
    with TenantTransaction(app_engine, tenant(1)) as tx:
        tx.commit()
        with pytest.raises(Exception, match="transaction_not_open"):
            _ = tx.session


def test_every_tenant_table_has_forced_rls_policy_and_tenant_leading_index(
    admin_engine: Engine,
) -> None:
    with admin_engine.connect() as conn:
        tables = conn.execute(text(TENANT_TABLES)).all()
        assert tables, "expected at least platform.jobs"
        for t in tables:
            assert (t.rls, t.forced) == (True, True), (
                f"{t.schema}.{t.table}: RLS must be enabled and FORCED"
            )
            policies: int = conn.execute(
                text("SELECT count(*) FROM pg_policies WHERE schemaname = :s AND tablename = :t"),
                {"s": t.schema, "t": t.table},
            ).scalar_one()
            assert policies >= 1, f"{t.schema}.{t.table}: no policy"
            leading: int = conn.execute(
                text(
                    "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0] "
                    "WHERE n.nspname = :s AND c.relname = :t AND a.attname = 'tenant_id'"
                ),
                {"s": t.schema, "t": t.table},
            ).scalar_one()
            assert leading >= 1, f"{t.schema}.{t.table}: needs an index leading with tenant_id"


def test_no_cross_schema_foreign_keys(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        bad = conn.execute(
            text(
                "SELECT conrelid::regclass::text AS a, confrelid::regclass::text AS b FROM pg_constraint c "
                "WHERE c.contype = 'f' AND (SELECT relnamespace FROM pg_class WHERE oid = c.conrelid) "
                "<> (SELECT relnamespace FROM pg_class WHERE oid = c.confrelid)"
            )
        ).all()
    assert bad == []
