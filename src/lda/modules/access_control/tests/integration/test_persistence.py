"""Real Postgres: schema ownership, forced RLS, privileges, the epoch trigger, atomicity."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.access_control.api import ActionId, GrantAccessCommand, ResourceRef
from lda.modules.access_control.domain.grant import Grant
from lda.modules.access_control.infrastructure.persistence.grant_repository import (
    SqlGrantRepository,
)
from lda.modules.access_control.infrastructure.persistence.unit_of_work import (
    SqlAccessControlUnitOfWorkFactory,
)
from lda.modules.access_control.tests.support import (
    ALICE,
    CAROL,
    TENANT_A,
    TENANT_B,
    Rig,
    ctx_for,
)
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId
from platform_support import Database

READ = ActionId("documents.read_document")
D1 = ResourceRef("document", "d1")
ROLE = "lda_app_access_control"
INSERT_GRANT = (
    "INSERT INTO access_control.grants (tenant_id, principal_id, resource_kind, resource_id, "
    "action, granted_at) VALUES (:t, gen_random_uuid(), :k, :i, :a, now())"
)


def insert_raw(engine: Engine, tenant: TenantId, **over: object) -> None:
    params: dict[str, object] = {
        "t": str(tenant),
        "k": "document",
        "i": "d1",
        "a": "documents.read_document",
        **over,
    }
    with TenantTransaction(engine, tenant) as tx:
        tx.session.execute(text(INSERT_GRANT), params)
        tx.commit()


def test_tables_live_only_in_the_access_control_schema(app_engine: Engine) -> None:
    with app_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, n.nspname FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE c.relname IN ('grants', 'policy_epochs') "
                "AND c.relkind = 'r'"
            )
        ).all()
    assert sorted(tuple(r) for r in rows) == [
        ("grants", "access_control"),
        ("policy_epochs", "access_control"),
    ]


def test_no_foreign_keys_exist_in_the_schema(app_engine: Engine) -> None:
    """ADR-014 §5/ADR-027: not even to the schema's own tables, and certainly not to identity."""
    with app_engine.connect() as conn:
        total = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace tn ON tn.oid = t.relnamespace "
                "WHERE c.contype = 'f' AND tn.nspname = 'access_control'"
            )
        ).scalar_one()
        inbound = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint c JOIN pg_class r ON r.oid = c.confrelid "
                "JOIN pg_namespace rn ON rn.oid = r.relnamespace "
                "WHERE c.contype = 'f' AND rn.nspname = 'access_control'"
            )
        ).scalar_one()
    assert (total, inbound) == (0, 0)


@pytest.mark.parametrize("table", ["grants", "policy_epochs"])
def test_rls_is_enabled_forced_and_has_a_policy(app_engine: Engine, table: str) -> None:
    with app_engine.connect() as conn:
        flags = conn.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'access_control' AND c.relname = :t"
            ),
            {"t": table},
        ).one()
        policies = conn.execute(
            text(
                "SELECT count(*) FROM pg_policies WHERE schemaname = 'access_control' "
                "AND tablename = :t"
            ),
            {"t": table},
        ).scalar_one()
    assert tuple(flags) == (True, True) and policies == 1


@pytest.mark.parametrize("table", ["grants", "policy_epochs"])
def test_primary_keys_lead_with_the_tenant(app_engine: Engine, table: str) -> None:
    with app_engine.connect() as conn:
        first = conn.execute(
            text(
                "SELECT a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = i.indkey[0] "
                "WHERE n.nspname = 'access_control' AND c.relname = :t AND i.indisprimary"
            ),
            {"t": table},
        ).scalar_one()
    assert first == "tenant_id"


def test_rls_alone_isolates_tenants_and_fails_closed(app_engine: Engine) -> None:
    insert_raw(app_engine, TENANT_A)
    insert_raw(app_engine, TENANT_B, i="d2")
    with TenantTransaction(app_engine, TENANT_A) as tx:
        rows = tx.session.execute(text("SELECT resource_id FROM access_control.grants")).all()
    assert [r[0] for r in rows] == ["d1"]  # no tenant predicate in the query: RLS did it
    with app_engine.connect() as conn:  # no scope set at all: nothing visible
        assert conn.execute(text("SELECT count(*) FROM access_control.grants")).scalar_one() == 0
        assert (
            conn.execute(text("SELECT count(*) FROM access_control.policy_epochs")).scalar_one()
            == 0
        )


def test_rls_rejects_writing_a_row_for_another_tenant(app_engine: Engine) -> None:
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(
            text(INSERT_GRANT),
            {"t": str(TENANT_B), "k": "document", "i": "x", "a": "documents.read_document"},
        )
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(
            text("INSERT INTO access_control.policy_epochs VALUES (:t, 1)"), {"t": str(TENANT_B)}
        )


@pytest.mark.parametrize(
    "over",
    [
        {"k": "Document"},
        {"k": ""},
        {"i": ""},
        {"i": "a b"},
        {"i": "-x"},
        {"a": "NoDots Allowed"},
        {"a": "documents."},
        {"a": "a." * 70 + "b"},
    ],
)
def test_check_constraints_reject_malformed_rows(
    app_engine: Engine, over: dict[str, object]
) -> None:
    with pytest.raises(DBAPIError):
        insert_raw(app_engine, TENANT_A, **over)


def test_the_primary_key_forbids_duplicate_grants(app_engine: Engine) -> None:
    principal = "11111111-1111-1111-1111-111111111111"
    stmt = text(INSERT_GRANT.replace("gen_random_uuid()", ":p"))
    with TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(
            stmt,
            {
                "t": str(TENANT_A),
                "p": principal,
                "k": "document",
                "i": "d1",
                "a": "documents.read_document",
            },
        )
        tx.commit()
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(
            stmt,
            {
                "t": str(TENANT_A),
                "p": principal,
                "k": "document",
                "i": "d1",
                "a": "documents.read_document",
            },
        )


def test_runtime_role_privileges_are_minimal(app_engine: Engine) -> None:
    """Grants are never updated; epochs are never deleted or truncated."""

    def attempt(sql: str, tenant: TenantId) -> None:
        with TenantTransaction(app_engine, tenant) as tx:
            tx.session.execute(text(sql))
            tx.commit()

    insert_raw(app_engine, TENANT_A)
    with pytest.raises(DBAPIError):
        attempt("UPDATE access_control.grants SET resource_id = 'zzz'", TENANT_A)
    with pytest.raises(DBAPIError):
        attempt("TRUNCATE access_control.grants", TENANT_A)
    with TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(
            text("INSERT INTO access_control.policy_epochs VALUES (:t, 1)"), {"t": str(TENANT_A)}
        )
        tx.commit()
    with pytest.raises(DBAPIError):
        attempt("DELETE FROM access_control.policy_epochs", TENANT_A)
    attempt("DELETE FROM access_control.grants", TENANT_A)  # revocation is a delete


def test_the_epoch_trigger_allows_only_plus_one(app_engine: Engine, admin_engine: Engine) -> None:
    with TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(
            text("INSERT INTO access_control.policy_epochs VALUES (:t, 1)"), {"t": str(TENANT_A)}
        )
        tx.commit()
    bad_updates = (
        "UPDATE access_control.policy_epochs SET epoch = epoch + 2",
        "UPDATE access_control.policy_epochs SET epoch = epoch",
        "UPDATE access_control.policy_epochs SET epoch = epoch - 1",
        "UPDATE access_control.policy_epochs SET epoch = 0",
    )
    for statement in bad_updates:
        with pytest.raises(DBAPIError), TenantTransaction(app_engine, TENANT_A) as tx:
            tx.session.execute(text(statement))
            tx.commit()
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, TENANT_A) as tx:  # re-homing
        tx.session.execute(
            text("UPDATE access_control.policy_epochs SET tenant_id = :o"), {"o": str(TENANT_B)}
        )
    with TenantTransaction(app_engine, TENANT_A) as tx:
        tx.session.execute(text("UPDATE access_control.policy_epochs SET epoch = epoch + 1"))
        tx.commit()
    with admin_engine.connect() as conn:  # even the table owner cannot rewind it
        with pytest.raises(DBAPIError):
            conn.execute(text("UPDATE access_control.policy_epochs SET epoch = 1"))
        conn.rollback()


def test_epoch_is_zero_until_the_first_change_and_then_counts(sql_rig: Rig) -> None:
    uow = sql_rig.policy.begin(ctx_for(TENANT_A, ALICE))
    with uow:
        assert uow.policy_epochs.current().value == 0
    for expected in (1, 2, 3):
        with sql_rig.policy.begin(ctx_for(TENANT_A, ALICE)) as uow:
            assert uow.policy_epochs.advance().value == expected
            uow.commit()
    with sql_rig.policy.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.policy_epochs.current().value == 3
    with sql_rig.policy.begin(ctx_for(TENANT_B, CAROL)) as uow:
        assert uow.policy_epochs.current().value == 0  # another tenant is untouched


def test_a_grant_and_its_epoch_move_commit_or_roll_back_together(sql_rig: Rig) -> None:
    ctx = ctx_for(TENANT_A, ALICE)
    with sql_rig.policy.begin(ctx) as uow:  # no commit: both vanish
        assert uow.grants.add(Grant(TENANT_A, ALICE, D1, READ, datetime.now(UTC)))
        uow.policy_epochs.advance()
    with sql_rig.policy.begin(ctx) as uow:
        assert uow.policy_epochs.current().value == 0
        assert uow.grants.granted(ALICE, READ, frozenset({D1})) == frozenset()
    with pytest.raises(RuntimeError), sql_rig.policy.begin(ctx) as uow:  # failure mid-way
        uow.grants.add(Grant(TENANT_A, ALICE, D1, READ, datetime.now(UTC)))
        uow.policy_epochs.advance()
        raise RuntimeError("boom")
    with sql_rig.policy.begin(ctx) as uow:
        assert uow.policy_epochs.current().value == 0
        assert uow.grants.granted(ALICE, READ, frozenset({D1})) == frozenset()


def test_a_failed_grant_use_case_leaves_neither_grant_nor_epoch(
    sql_rig: Rig, admin_engine: Engine
) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text(f"REVOKE INSERT ON access_control.policy_epochs FROM {ROLE}"))
    try:
        with pytest.raises(InfrastructureError):
            sql_rig.grant.execute(ctx_for(TENANT_A, ALICE), GrantAccessCommand(ALICE, D1, READ))
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text(f"GRANT INSERT ON access_control.policy_epochs TO {ROLE}"))
    with sql_rig.policy.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.granted(ALICE, READ, frozenset({D1})) == frozenset()


def test_concurrent_changes_each_advance_the_epoch_exactly_once(sql_rig: Rig) -> None:
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            sql_rig.grant.execute(
                ctx_for(TENANT_A, ALICE),
                GrantAccessCommand(ALICE, ResourceRef("document", f"c{n}"), READ),
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with sql_rig.policy.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.policy_epochs.current().value == 8  # no lost update, no skipped value


def test_repository_predicates_isolate_tenants_even_without_rls(
    admin_engine: Engine, sql_rig: Rig
) -> None:
    """Defence in depth: through a superuser connection RLS is bypassed; the predicate must hold."""
    with admin_engine.begin() as conn:
        conn.execute(
            text(INSERT_GRANT.replace("gen_random_uuid()", ":p")),
            {
                "t": str(TENANT_B),
                "p": str(ALICE.value),
                "k": "document",
                "i": "d1",
                "a": "documents.read_document",
            },
        )
    with TenantTransaction(admin_engine, TENANT_A) as scoped:
        repository = SqlGrantRepository(scoped)
        assert repository.granted(ALICE, READ, frozenset({D1})) == frozenset()
        assert repository.remove(ALICE, D1, READ) is False  # tenant B's row is untouched
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM access_control.grants")).scalar_one() == 1


def test_a_grant_for_another_tenant_cannot_be_added_through_a_bound_repository(
    app_engine: Engine,
) -> None:
    with (
        TenantTransaction(app_engine, TENANT_A) as tx,
        pytest.raises(InfrastructureError) as caught,
    ):
        SqlGrantRepository(tx).add(Grant(TENANT_B, ALICE, D1, READ, datetime.now(UTC)))
    assert caught.value.code == "grant_tenant_mismatch"


def test_driver_errors_are_translated_without_leaking_resource_ids(
    sql_policy_factory: SqlAccessControlUnitOfWorkFactory, admin_engine: Engine
) -> None:
    confidential_resource_id = "client-matter-4711-confidential"
    with admin_engine.begin() as conn:
        conn.execute(text(f"REVOKE INSERT ON access_control.grants FROM {ROLE}"))
    try:
        with (
            pytest.raises(InfrastructureError) as info,
            sql_policy_factory.begin(ctx_for(TENANT_A, ALICE)) as uow,
        ):
            uow.grants.add(
                Grant(
                    TENANT_A,
                    ALICE,
                    ResourceRef("document", confidential_resource_id),
                    READ,
                    datetime.now(UTC),
                )
            )
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text(f"GRANT SELECT, INSERT, DELETE ON access_control.grants TO {ROLE}"))
    error = info.value
    assert error.code == "access_control_persistence_failed"
    assert confidential_resource_id not in str(error) + error.detail + repr(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert error.__context__ is None or confidential_resource_id not in repr(error.__context__.args)


def test_large_candidate_sets_resolve_correctly(sql_rig: Rig) -> None:
    ctx = ctx_for(TENANT_A, ALICE)
    with sql_rig.policy.begin(ctx) as uow:
        for n in range(0, 2000, 2):  # grant the even ones
            uow.grants.add(
                Grant(TENANT_A, ALICE, ResourceRef("document", f"n{n}"), READ, datetime.now(UTC))
            )
        uow.commit()
    candidates = frozenset(ResourceRef("document", f"n{n}") for n in range(2000))
    with sql_rig.policy.begin(ctx) as uow:
        granted = uow.grants.granted(ALICE, READ, candidates)
    assert granted == frozenset(ResourceRef("document", f"n{n}") for n in range(0, 2000, 2))


def test_migration_downgrade_and_upgrade_round_trip(database: Database) -> None:
    from lda.modules.access_control.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
    from lda.platform.database import migrate

    locations = [MIGRATIONS_VERSIONS]
    migrate.downgrade(
        database.admin_url, target="access_control@base", app_role=ROLE, extra_locations=locations
    )
    migrate.upgrade(database.admin_url, app_role=ROLE, extra_locations=locations)


def test_epoch_reads_and_writes_are_tenant_predicated_even_without_rls(
    admin_engine: Engine,
) -> None:
    """Superuser connections bypass RLS: the repository's own predicate must still hold."""
    from lda.modules.access_control.infrastructure.persistence.epoch_repository import (
        SqlPolicyEpochRepository,
    )

    with admin_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO access_control.policy_epochs VALUES (:t, 7)"), {"t": str(TENANT_B)}
        )
    with TenantTransaction(admin_engine, TENANT_A) as scoped:
        repository = SqlPolicyEpochRepository(scoped)
        assert repository.current().value == 0  # tenant B's row (7) must not leak in
        assert repository.advance().value == 1  # and A starts its own count
        scoped.commit()
    with admin_engine.connect() as conn:
        query = text("SELECT tenant_id::text, epoch FROM access_control.policy_epochs")
        rows: dict[str, int] = {str(tid): int(epoch) for tid, epoch in conn.execute(query).all()}
    assert rows == {str(TENANT_A): 1, str(TENANT_B): 7}
