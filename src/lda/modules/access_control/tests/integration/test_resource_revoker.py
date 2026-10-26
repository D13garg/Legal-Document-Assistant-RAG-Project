"""`SqlResourceAccessRevoker` (ADR-023): grants and epoch change on the CALLER's transaction."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text

from lda.modules.access_control.api import ActionId, ResourceRef
from lda.modules.access_control.domain.errors import InvalidResourceRef
from lda.modules.access_control.domain.grant import Grant
from lda.modules.access_control.infrastructure.persistence.epoch_repository import (
    SqlPolicyEpochRepository,
)
from lda.modules.access_control.infrastructure.persistence.grant_repository import (
    SqlGrantRepository,
)
from lda.modules.access_control.infrastructure.persistence.unit_of_work import (
    SqlResourceAccessRevoker,
)
from lda.modules.access_control.tests.support import ALICE, BOB, TENANT_A, TENANT_B
from lda.platform.database.transaction import TenantTransaction
from lda.shared.ports.access_revoker import ResourceAccessRevoker

READ, DELETE = ActionId("documents.read_document"), ActionId("documents.delete_document")
D1, D2 = ResourceRef("document", "d1"), ResourceRef("document", "d2")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def seed(
    engine: Engine, tenant: object, grants: list[tuple[object, ResourceRef, ActionId]]
) -> None:
    with TenantTransaction(engine, tenant) as tx:  # type: ignore[arg-type]
        repo = SqlGrantRepository(tx)
        for principal, resource, action in grants:
            repo.add(Grant(tenant, principal, resource, action, NOW))  # type: ignore[arg-type]
        tx.commit()


def state(engine: Engine, tenant: object) -> tuple[int, int]:
    with TenantTransaction(engine, tenant) as tx:  # type: ignore[arg-type]
        count = tx.session.execute(text("SELECT count(*) FROM access_control.grants")).scalar_one()
        return int(count), SqlPolicyEpochRepository(tx).current().value


def test_it_satisfies_the_shared_port() -> None:
    revoker: type[ResourceAccessRevoker] = SqlResourceAccessRevoker
    assert callable(revoker.revoke_all)


def test_all_grants_go_and_the_epoch_advances_exactly_once(app_engine: Engine) -> None:
    seed(
        app_engine,
        TENANT_A,
        [(ALICE, D1, READ), (ALICE, D1, DELETE), (BOB, D1, READ), (BOB, D2, READ)],
    )
    with TenantTransaction(app_engine, TENANT_A) as tx:
        assert SqlResourceAccessRevoker(tx).revoke_all("document", "d1") == 3
        tx.commit()
    assert state(app_engine, TENANT_A) == (1, 1)  # D2 survives; epoch 0 -> 1, once for 3 grants


def test_nothing_to_revoke_changes_nothing_and_does_not_advance_the_epoch(
    app_engine: Engine,
) -> None:
    seed(app_engine, TENANT_A, [(ALICE, D2, READ)])
    with TenantTransaction(app_engine, TENANT_A) as tx:
        assert SqlResourceAccessRevoker(tx).revoke_all("document", "d1") == 0
        tx.commit()
    assert state(app_engine, TENANT_A) == (1, 0)


def test_it_never_commits_and_a_rollback_restores_grants_and_epoch(app_engine: Engine) -> None:
    seed(app_engine, TENANT_A, [(ALICE, D1, READ)])
    with TenantTransaction(app_engine, TENANT_A) as tx:
        assert SqlResourceAccessRevoker(tx).revoke_all("document", "d1") == 1
        # no commit: the caller's action failed
    assert state(app_engine, TENANT_A) == (1, 0)


def test_it_is_bound_to_its_tenant(app_engine: Engine) -> None:
    seed(app_engine, TENANT_A, [(ALICE, D1, READ)])
    seed(app_engine, TENANT_B, [(ALICE, D1, READ)])
    with TenantTransaction(app_engine, TENANT_B) as tx:
        assert SqlResourceAccessRevoker(tx).revoke_all("document", "d1") == 1
        tx.commit()
    assert state(app_engine, TENANT_A) == (1, 0)
    assert state(app_engine, TENANT_B) == (0, 1)


@pytest.mark.parametrize(
    ("kind", "rid"), [("Bad Kind", "d1"), ("document", ""), ("document", "a b")]
)
def test_an_invalid_resource_is_rejected_before_any_sql(
    app_engine: Engine, kind: str, rid: str
) -> None:
    seed(app_engine, TENANT_A, [(ALICE, D1, READ)])
    with TenantTransaction(app_engine, TENANT_A) as tx, pytest.raises(InvalidResourceRef):
        SqlResourceAccessRevoker(tx).revoke_all(kind, rid)
    assert state(app_engine, TENANT_A) == (1, 0)
