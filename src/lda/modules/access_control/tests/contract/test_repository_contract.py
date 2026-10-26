"""One behavioural contract, run against the in-memory and the SQL unit of work."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lda.modules.access_control.api import ActionId, ResourceRef
from lda.modules.access_control.application.ports import AccessControlUnitOfWorkFactory
from lda.modules.access_control.domain.grant import Grant
from lda.modules.access_control.tests.support import ALICE, BOB, CAROL, TENANT_A, TENANT_B, ctx_for
from lda.shared.kernel.errors import InfrastructureError

READ, DELETE = ActionId("documents.read_document"), ActionId("documents.delete_document")
D1, D2 = ResourceRef("document", "d1"), ResourceRef("document", "d2")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(params=["memory", "sql"])
def factory(request: pytest.FixtureRequest) -> AccessControlUnitOfWorkFactory:
    name = "memory_policy_factory" if request.param == "memory" else "sql_policy_factory"
    value: AccessControlUnitOfWorkFactory = request.getfixturevalue(name)
    return value


def commit_grant(factory: AccessControlUnitOfWorkFactory, resource: ResourceRef = D1) -> None:
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        uow.grants.add(Grant(TENANT_A, ALICE, resource, READ, NOW))
        uow.commit()


def test_add_reports_whether_anything_changed(factory: AccessControlUnitOfWorkFactory) -> None:
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.add(Grant(TENANT_A, ALICE, D1, READ, NOW)) is True
        assert uow.grants.add(Grant(TENANT_A, ALICE, D1, READ, NOW)) is False
        assert uow.grants.add(Grant(TENANT_A, ALICE, D1, DELETE, NOW)) is True
        uow.commit()


def test_remove_reports_whether_anything_changed(factory: AccessControlUnitOfWorkFactory) -> None:
    commit_grant(factory)
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.remove(ALICE, D1, READ) is True
        assert uow.grants.remove(ALICE, D1, READ) is False
        assert uow.grants.remove(ALICE, D2, READ) is False
        uow.commit()


def test_granted_returns_only_held_candidates(factory: AccessControlUnitOfWorkFactory) -> None:
    commit_grant(factory, D1)
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.granted(ALICE, READ, frozenset({D1, D2})) == frozenset({D1})
        assert uow.grants.granted(ALICE, DELETE, frozenset({D1})) == frozenset()
        assert uow.grants.granted(BOB, READ, frozenset({D1})) == frozenset()
        assert uow.grants.granted(ALICE, READ, frozenset()) == frozenset()


def test_granted_never_returns_more_than_asked(factory: AccessControlUnitOfWorkFactory) -> None:
    for resource in (D1, D2):
        commit_grant(factory, resource)
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.granted(ALICE, READ, frozenset({D2})) == frozenset({D2})


def test_tenants_are_isolated(factory: AccessControlUnitOfWorkFactory) -> None:
    commit_grant(factory)
    with factory.begin(ctx_for(TENANT_B, CAROL)) as uow:
        assert uow.grants.granted(ALICE, READ, frozenset({D1})) == frozenset()
        assert uow.grants.remove(ALICE, D1, READ) is False
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.granted(ALICE, READ, frozenset({D1})) == frozenset({D1})


def test_a_bound_repository_refuses_another_tenants_grant(
    factory: AccessControlUnitOfWorkFactory,
) -> None:
    ctx = ctx_for(TENANT_A, ALICE)
    with factory.begin(ctx) as uow, pytest.raises(InfrastructureError) as caught:
        uow.grants.add(Grant(TENANT_B, ALICE, D1, READ, NOW))
    assert caught.value.code == "grant_tenant_mismatch"


def test_uncommitted_work_is_rolled_back(factory: AccessControlUnitOfWorkFactory) -> None:
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        uow.grants.add(Grant(TENANT_A, ALICE, D1, READ, NOW))
        uow.policy_epochs.advance()
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.granted(ALICE, READ, frozenset({D1})) == frozenset()
        assert uow.policy_epochs.current().value == 0


def test_epoch_starts_at_zero_counts_by_one_and_is_per_tenant(
    factory: AccessControlUnitOfWorkFactory,
) -> None:
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.policy_epochs.current().value == 0
        assert [uow.policy_epochs.advance().value for _ in range(3)] == [1, 2, 3]
        assert uow.policy_epochs.current().value == 3  # visible inside the transaction
        uow.commit()
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.policy_epochs.current().value == 3
    with factory.begin(ctx_for(TENANT_B, CAROL)) as uow:
        assert uow.policy_epochs.current().value == 0


def test_remove_all_for_resource_removes_every_principal_and_action(
    factory: AccessControlUnitOfWorkFactory,
) -> None:
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        for principal, action in ((ALICE, READ), (ALICE, DELETE), (BOB, READ)):
            uow.grants.add(Grant(TENANT_A, principal, D1, action, NOW))
        uow.grants.add(Grant(TENANT_A, ALICE, D2, READ, NOW))
        uow.commit()
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.remove_all_for_resource(D1) == 3
        assert uow.grants.remove_all_for_resource(D1) == 0  # nothing left: idempotent
        assert uow.grants.granted(ALICE, READ, frozenset({D1, D2})) == frozenset({D2})
        uow.commit()
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.granted(BOB, READ, frozenset({D1})) == frozenset()
        assert uow.grants.granted(ALICE, DELETE, frozenset({D1})) == frozenset()


def test_remove_all_for_resource_never_touches_another_tenant(
    factory: AccessControlUnitOfWorkFactory,
) -> None:
    commit_grant(factory, D1)
    with factory.begin(ctx_for(TENANT_B, CAROL)) as uow:
        assert uow.grants.remove_all_for_resource(D1) == 0
        uow.commit()
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.granted(ALICE, READ, frozenset({D1})) == frozenset({D1})


def test_remove_all_for_resource_is_rolled_back_with_the_unit(
    factory: AccessControlUnitOfWorkFactory,
) -> None:
    commit_grant(factory, D1)
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.remove_all_for_resource(D1) == 1
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.granted(ALICE, READ, frozenset({D1})) == frozenset({D1})


def test_remove_all_for_resource_sees_grants_added_in_the_same_unit(
    factory: AccessControlUnitOfWorkFactory,
) -> None:
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        uow.grants.add(Grant(TENANT_A, ALICE, D1, READ, NOW))
        assert uow.grants.remove_all_for_resource(D1) == 1
        assert uow.grants.granted(ALICE, READ, frozenset({D1})) == frozenset()


def test_remove_all_for_resource_matches_kind_and_id_not_just_the_id(
    factory: AccessControlUnitOfWorkFactory,
) -> None:
    same_id_other_kind = ResourceRef("matter", D1.id)
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        uow.grants.add(Grant(TENANT_A, ALICE, D1, READ, NOW))
        uow.grants.add(Grant(TENANT_A, ALICE, same_id_other_kind, READ, NOW))
        uow.commit()
    with factory.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.grants.remove_all_for_resource(D1) == 1
        assert uow.grants.granted(ALICE, READ, frozenset({same_id_other_kind})) == frozenset(
            {same_id_other_kind}
        )
        uow.commit()
