"""One behavioural contract, run against the in-memory and the SQL unit of work."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.identity.application.ports import IdentityUnitOfWorkFactory
from lda.modules.identity.domain.principal import PrincipalStatus
from lda.modules.identity.testing import a_principal
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.ids import PrincipalId, TenantId

PID = PrincipalId(UUID(int=0xC0))


@pytest.fixture(params=["memory", "sql"])
def factory(request: pytest.FixtureRequest) -> IdentityUnitOfWorkFactory:
    fixture: IdentityUnitOfWorkFactory = request.getfixturevalue(f"{request.param}_factory")
    return fixture


def commit_new(factory: IdentityUnitOfWorkFactory, ctx: ExecutionContext) -> None:
    with factory.begin(ctx) as uow:
        uow.principals.add(a_principal(tenant_id=ctx.tenant_id, id=PID))
        uow.commit()


def test_round_trip(factory: IdentityUnitOfWorkFactory, ctx_a: ExecutionContext) -> None:
    commit_new(factory, ctx_a)
    with factory.begin(ctx_a) as uow:
        assert uow.principals.get(PID) == a_principal(tenant_id=ctx_a.tenant_id, id=PID)


def test_unknown_id_is_none(factory: IdentityUnitOfWorkFactory, ctx_a: ExecutionContext) -> None:
    with factory.begin(ctx_a) as uow:
        assert uow.principals.get(PID) is None


def test_uncommitted_work_is_rolled_back(
    factory: IdentityUnitOfWorkFactory, ctx_a: ExecutionContext
) -> None:
    with factory.begin(ctx_a) as uow:
        uow.principals.add(a_principal(tenant_id=ctx_a.tenant_id, id=PID))
    with factory.begin(ctx_a) as uow:
        assert uow.principals.get(PID) is None


def test_exception_inside_the_block_rolls_back(
    factory: IdentityUnitOfWorkFactory, ctx_a: ExecutionContext
) -> None:
    def fail() -> None:
        with factory.begin(ctx_a) as uow:
            uow.principals.add(a_principal(tenant_id=ctx_a.tenant_id, id=PID))
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        fail()
    with factory.begin(ctx_a) as uow:
        assert uow.principals.get(PID) is None


def test_duplicate_id_conflicts(
    factory: IdentityUnitOfWorkFactory, ctx_a: ExecutionContext
) -> None:
    commit_new(factory, ctx_a)
    with pytest.raises(ConflictError) as info, factory.begin(ctx_a) as uow:
        uow.principals.add(a_principal(tenant_id=ctx_a.tenant_id, id=PID))
    assert info.value.code == "principal_exists"


def test_another_tenant_cannot_see_reuse_or_change_the_principal(
    factory: IdentityUnitOfWorkFactory, ctx_a: ExecutionContext, ctx_b: ExecutionContext
) -> None:
    commit_new(factory, ctx_a)
    with factory.begin(ctx_b) as uow:
        assert uow.principals.get(PID) is None
        original = a_principal(tenant_id=ctx_a.tenant_id, id=PID)
        with pytest.raises(ConflictError) as info:
            uow.principals.save(original.disable(original.created_at + timedelta(seconds=1)))
        assert info.value.code == "principal_state_changed"
    with factory.begin(ctx_a) as uow:  # tenant A's row is untouched
        stored = uow.principals.get(PID)
        assert stored is not None and stored.status is PrincipalStatus.ACTIVE


def test_a_repository_refuses_a_principal_of_another_tenant(
    factory: IdentityUnitOfWorkFactory, ctx_b: ExecutionContext, tenant_a: TenantId
) -> None:
    with pytest.raises(InfrastructureError) as info, factory.begin(ctx_b) as uow:
        uow.principals.add(a_principal(tenant_id=tenant_a, id=PID))
    assert info.value.code == "principal_tenant_mismatch"


def test_save_is_optimistic(factory: IdentityUnitOfWorkFactory, ctx_a: ExecutionContext) -> None:
    commit_new(factory, ctx_a)
    base = a_principal(tenant_id=ctx_a.tenant_id, id=PID)
    later = base.created_at + timedelta(seconds=1)
    with factory.begin(ctx_a) as uow:
        uow.principals.save(base.disable(later))
        uow.commit()
    with pytest.raises(ConflictError) as info, factory.begin(ctx_a) as uow:
        uow.principals.save(base.disable(later))  # started from a stale version
    assert info.value.code == "principal_state_changed"
    with factory.begin(ctx_a) as uow:
        stored = uow.principals.get(PID)
        assert stored is not None and (stored.status, stored.version) == (
            PrincipalStatus.DISABLED,
            2,
        )


def test_save_of_a_missing_principal_conflicts(
    factory: IdentityUnitOfWorkFactory, ctx_a: ExecutionContext
) -> None:
    base = a_principal(tenant_id=ctx_a.tenant_id, id=PID)
    with pytest.raises(ConflictError), factory.begin(ctx_a) as uow:
        uow.principals.save(base.disable(base.created_at))


def test_unit_of_work_is_bound_to_the_context_tenant(
    factory: IdentityUnitOfWorkFactory, ctx_a: ExecutionContext, ctx_b: ExecutionContext
) -> None:
    with factory.begin(ctx_a) as uow:
        assert uow.tenant_id == ctx_a.tenant_id
    with factory.begin(ctx_b) as uow:
        assert uow.tenant_id == ctx_b.tenant_id
