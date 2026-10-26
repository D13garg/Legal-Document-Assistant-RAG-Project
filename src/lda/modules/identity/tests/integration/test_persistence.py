"""Real Postgres: schema ownership, constraints, forced RLS, transactions, error hygiene."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.identity.infrastructure.persistence.tables import principals
from lda.modules.identity.infrastructure.persistence.unit_of_work import (
    SqlIdentityUnitOfWorkFactory,
)
from lda.modules.identity.testing import a_principal
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.ids import PrincipalId, TenantId
from platform_support import Database

PID = PrincipalId(UUID(int=0xC0))
INSERT = (
    "INSERT INTO identity.principals (id, tenant_id, kind, display_name, status, version, "
    "created_at, status_changed_at) VALUES (gen_random_uuid(), :t, :k, :n, :s, :v, now(), now())"
)


def insert_raw(engine: Engine, tenant: TenantId, **over: object) -> None:
    params: dict[str, object] = {
        "t": str(tenant),
        "k": "user",
        "n": "name",
        "s": "active",
        "v": 1,
        **over,
    }
    with TenantTransaction(engine, tenant) as tx:
        tx.session.execute(text(INSERT), params)
        tx.commit()


def test_table_lives_only_in_the_identity_schema(app_engine: Engine) -> None:
    assert principals.schema == "identity"
    with app_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = 'principals'"
            )
        ).all()
    assert [r[0] for r in rows] == ["identity"]


def test_no_foreign_keys_leave_the_schema(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
                "JOIN pg_class r ON r.oid = c.confrelid "
                "JOIN pg_namespace rn ON rn.oid = r.relnamespace "
                "WHERE c.contype = 'f' AND tn.nspname = 'identity' AND rn.nspname <> 'identity'"
            )
        ).scalar_one()
    assert rows == 0


def test_rls_is_enabled_and_forced(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'identity' AND c.relname = 'principals'"
            )
        ).one()
    assert tuple(row) == (True, True)


def _count_without_predicate(engine: Engine, scope: dict[str, str]) -> int:
    with engine.connect() as conn, conn.begin():
        for name, value in scope.items():
            conn.execute(text("SELECT set_config(:n, :v, true)"), {"n": name, "v": value})
        return int(conn.execute(text("SELECT count(*) FROM identity.principals")).scalar_one())


def test_rls_alone_isolates_tenants_and_fails_closed(
    app_engine: Engine, tenant_a: TenantId, tenant_b: TenantId
) -> None:
    insert_raw(app_engine, tenant_a)
    insert_raw(app_engine, tenant_b)
    insert_raw(app_engine, tenant_b)
    assert _count_without_predicate(app_engine, {"app.tenant_id": str(tenant_a)}) == 1
    assert _count_without_predicate(app_engine, {"app.tenant_id": str(tenant_b)}) == 2
    assert _count_without_predicate(app_engine, {}) == 0
    assert _count_without_predicate(app_engine, {"app.tenant_id": ""}) == 0


def test_rls_rejects_writing_a_row_for_another_tenant(
    app_engine: Engine, tenant_a: TenantId, tenant_b: TenantId
) -> None:
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, tenant_a) as tx:
        tx.session.execute(
            text(INSERT), {"t": str(tenant_b), "k": "user", "n": "n", "s": "active", "v": 1}
        )


@pytest.mark.parametrize(
    "bad",
    [{"k": "robot"}, {"s": "deleted"}, {"v": 0}, {"n": ""}, {"n": "x" * 201}],
)
def test_check_constraints_reject_invalid_rows(
    app_engine: Engine, tenant_a: TenantId, bad: dict[str, object]
) -> None:
    with pytest.raises(DBAPIError):
        insert_raw(app_engine, tenant_a, **bad)


def test_status_changed_at_cannot_precede_created_at(
    app_engine: Engine, tenant_a: TenantId
) -> None:
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, tenant_a) as tx:
        tx.session.execute(
            text(
                "INSERT INTO identity.principals VALUES (gen_random_uuid(), :t, 'user', 'n', "
                "'active', 1, now(), now() - interval '1 second')"
            ),
            {"t": str(tenant_a)},
        )


def test_runtime_role_cannot_delete_principals(app_engine: Engine, tenant_a: TenantId) -> None:
    insert_raw(app_engine, tenant_a)
    with (
        pytest.raises(DBAPIError, match="permission denied"),
        TenantTransaction(app_engine, tenant_a) as tx,
    ):
        tx.session.execute(text("DELETE FROM identity.principals"))


def test_commit_and_rollback_through_the_sql_unit_of_work(
    sql_factory: SqlIdentityUnitOfWorkFactory, ctx_a: ExecutionContext
) -> None:
    with sql_factory.begin(ctx_a) as uow:
        uow.principals.add(a_principal(tenant_id=ctx_a.tenant_id, id=PID))
        uow.rollback()
    with sql_factory.begin(ctx_a) as uow:
        assert uow.principals.get(PID) is None
        uow.principals.add(a_principal(tenant_id=ctx_a.tenant_id, id=PID))
        uow.commit()
    with sql_factory.begin(ctx_a) as uow:
        assert uow.principals.get(PID) is not None


def test_driver_errors_are_translated_without_leaking_parameters(
    sql_factory: SqlIdentityUnitOfWorkFactory, ctx_a: ExecutionContext, admin_engine: Engine
) -> None:
    personal_name = "Jane Q. Confidential"
    role = "lda_app_identity"
    with admin_engine.begin() as conn:
        conn.execute(text(f"REVOKE INSERT ON identity.principals FROM {role}"))
    try:
        with pytest.raises(InfrastructureError) as info, sql_factory.begin(ctx_a) as uow:
            uow.principals.add(
                a_principal(tenant_id=ctx_a.tenant_id, id=PID, display_name=personal_name)
            )
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text(f"GRANT INSERT ON identity.principals TO {role}"))
    error = info.value
    assert error.code == "identity_persistence_failed"
    assert personal_name not in str(error) + error.detail + repr(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert error.__context__ is None or personal_name not in repr(error.__context__.args)


def test_migration_downgrade_and_upgrade_round_trip(database: Database) -> None:
    from lda.modules.identity.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
    from lda.platform.database import migrate

    role, locations = "lda_app_identity", [MIGRATIONS_VERSIONS]
    migrate.downgrade(
        database.admin_url, target="identity@base", app_role=role, extra_locations=locations
    )
    migrate.upgrade(database.admin_url, app_role=role, extra_locations=locations)


def test_repository_predicates_isolate_tenants_even_without_rls(
    admin_engine: Engine, ctx_a: ExecutionContext, tenant_b: TenantId
) -> None:
    """Defence in depth: through a superuser connection RLS is bypassed, the predicate must hold."""
    from datetime import timedelta

    from lda.modules.identity.infrastructure.persistence.principal_repository import (
        SqlPrincipalRepository,
    )

    with admin_engine.connect() as conn:
        assert conn.execute(
            text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        ).scalar_one()
    with TenantTransaction(admin_engine, ctx_a.tenant_id) as tx:
        SqlPrincipalRepository(tx).add(a_principal(tenant_id=ctx_a.tenant_id, id=PID))
        tx.commit()
    base = a_principal(tenant_id=ctx_a.tenant_id, id=PID)
    with TenantTransaction(admin_engine, tenant_b) as tx:
        repo = SqlPrincipalRepository(tx)
        assert repo.get(PID) is None
        with pytest.raises(ConflictError):
            repo.save(base.disable(base.created_at + timedelta(seconds=1)))
    with TenantTransaction(admin_engine, ctx_a.tenant_id) as tx:
        stored = SqlPrincipalRepository(tx).get(PID)
        assert stored is not None and stored.is_active
