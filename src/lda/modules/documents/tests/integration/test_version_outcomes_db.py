"""The database enforces the version lifecycle too (ADR-015 amendment): pending -> ready | failed |
quarantined, once; terminal states never change; a tombstoned document's version never becomes
ready; only `status` may be updated by the runtime role."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.documents.infrastructure.persistence.unit_of_work import (
    SqlDocumentsUnitOfWorkFactory,
)
from lda.modules.documents.tests.contract.test_repository_contract import seed, tombstone
from lda.modules.documents.tests.support import TENANT_A, TENANT_B
from lda.platform.database.transaction import TenantTransaction
from platform_support import Database


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def run(app_engine: Engine, statement: str, tenant=TENANT_A) -> None:  # type: ignore[no-untyped-def]
    with TenantTransaction(app_engine, tenant) as tx:
        tx.session.execute(text(statement))
        tx.commit()


def status(admin_engine: Engine) -> str:
    with admin_engine.connect() as conn:
        return str(conn.execute(text("SELECT status FROM documents.versions")).scalar_one())


@pytest.mark.parametrize("outcome", ["ready", "failed", "quarantined"])
def test_a_pending_version_can_take_each_terminal_outcome(
    sql_factory: SqlDocumentsUnitOfWorkFactory,
    app_engine: Engine,
    admin_engine: Engine,
    outcome: str,
) -> None:
    seed(sql_factory)
    run(app_engine, f"UPDATE documents.versions SET status = '{outcome}'")  # noqa: S608
    assert status(admin_engine) == outcome


CHANGES = [
    (first, second)
    for first in ("ready", "failed", "quarantined")
    for second in ("pending", "ready", "failed", "quarantined")
    if first != second
]


@pytest.mark.parametrize(("first", "second"), CHANGES)
def test_a_terminal_outcome_never_changes_again(
    sql_factory: SqlDocumentsUnitOfWorkFactory,
    app_engine: Engine,
    admin_engine: Engine,
    first: str,
    second: str,
) -> None:
    seed(sql_factory)
    run(app_engine, f"UPDATE documents.versions SET status = '{first}'")  # noqa: S608
    with pytest.raises(DBAPIError) as caught:
        run(app_engine, f"UPDATE documents.versions SET status = '{second}'")  # noqa: S608
    assert sqlstate(caught.value) == "55000"
    assert status(admin_engine) == first


def test_a_version_of_a_deleted_document_can_never_become_ready(
    sql_factory: SqlDocumentsUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:
    seed(sql_factory)
    tombstone(sql_factory)
    with pytest.raises(DBAPIError) as caught:
        run(app_engine, "UPDATE documents.versions SET status = 'ready'")
    assert sqlstate(caught.value) == "55000"
    assert status(admin_engine) == "pending"
    run(app_engine, "UPDATE documents.versions SET status = 'failed'")  # not a ready claim
    assert status(admin_engine) == "failed"


def test_an_unknown_status_value_is_rejected_by_the_check_constraint(
    sql_factory: SqlDocumentsUnitOfWorkFactory, app_engine: Engine
) -> None:
    seed(sql_factory)
    for bad in ("READY", "done", ""):
        with pytest.raises(DBAPIError) as caught:
            run(app_engine, f"UPDATE documents.versions SET status = '{bad}'")  # noqa: S608
        assert sqlstate(caught.value) == "23514"


def test_the_runtime_role_may_update_only_the_status_column(
    sql_factory: SqlDocumentsUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:
    seed(sql_factory)
    with admin_engine.connect() as conn:
        columns = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.column_privileges "
                    "WHERE table_schema = 'documents' AND table_name = 'versions' "
                    "AND grantee = 'lda_app_documents' AND privilege_type = 'UPDATE'"
                )
            )
        }
    assert columns == {"status"}
    for column, value in (
        ("size_bytes", "1"),
        ("media_type", "'text/plain'"),
        ("number", "9"),
        ("created_at", "now()"),
    ):
        with pytest.raises(DBAPIError) as caught:
            run(app_engine, f"UPDATE documents.versions SET {column} = {value}")  # noqa: S608
        assert sqlstate(caught.value) == "42501", column


def test_other_columns_stay_immutable_even_for_the_table_owner(
    sql_factory: SqlDocumentsUnitOfWorkFactory, admin_engine: Engine
) -> None:
    seed(sql_factory)
    for statement in (
        "UPDATE documents.versions SET size_bytes = size_bytes + 1",
        "UPDATE documents.versions SET number = 2",
        "UPDATE documents.versions SET media_type = 'text/plain'",
    ):
        with pytest.raises(DBAPIError) as caught, admin_engine.begin() as conn:
            conn.execute(text(statement))
        assert sqlstate(caught.value) == "55000", statement


def test_a_tenant_cannot_change_another_tenants_version_status(
    sql_factory: SqlDocumentsUnitOfWorkFactory, app_engine: Engine, admin_engine: Engine
) -> None:
    seed(sql_factory)
    with TenantTransaction(app_engine, TENANT_B) as tx:
        changed = tx.session.execute(
            text("UPDATE documents.versions SET status = 'ready' RETURNING version_id")
        ).all()
        tx.commit()
    assert changed == []
    assert status(admin_engine) == "pending"


def test_two_concurrent_outcomes_leave_exactly_one_winner(
    sql_factory: SqlDocumentsUnitOfWorkFactory, admin_engine: Engine
) -> None:
    """Real Postgres: the compare-and-set on `pending` means only one of two competing outcomes
    (a lost-lease executor racing its successor, say) can be recorded."""
    import threading

    from lda.modules.documents.domain.document import VersionStatus
    from lda.modules.documents.tests.contract.test_repository_contract import V1
    from lda.modules.documents.tests.support import ctx_for

    seed(sql_factory)
    barrier = threading.Barrier(2, timeout=20)
    wins: list[VersionStatus] = []
    errors: list[BaseException] = []

    def record(outcome: VersionStatus) -> None:
        try:
            with sql_factory.begin(ctx_for(TENANT_A)) as uow:
                barrier.wait()
                if uow.versions.set_outcome(V1, outcome):
                    uow.commit()
                    wins.append(outcome)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=record, args=(o,))
        for o in (VersionStatus.READY, VersionStatus.QUARANTINED)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(wins) == 1
    assert status(admin_engine) == wins[0].value


def test_set_outcome_carries_its_own_tenant_predicate_even_without_rls(
    sql_factory: SqlDocumentsUnitOfWorkFactory,
    database: Database,
    admin_engine: Engine,
) -> None:
    """A superuser bypasses RLS: a tenant-B repository must still not move tenant A's version."""
    from sqlalchemy import create_engine

    from lda.modules.documents.domain.document import VersionStatus
    from lda.modules.documents.infrastructure.persistence.repositories import (
        SqlVersionRepository,
    )
    from lda.modules.documents.tests.contract.test_repository_contract import V1

    seed(sql_factory)
    superuser = create_engine(database.admin_url)
    try:
        with TenantTransaction(superuser, TENANT_B) as tx:
            assert SqlVersionRepository(tx).set_outcome(V1, VersionStatus.READY) is False
            tx.commit()
    finally:
        superuser.dispose()
    assert status(admin_engine) == "pending"
