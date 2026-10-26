from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.identity.api import (
    DisablePrincipal,
    EnablePrincipal,
    GetPrincipal,
    PrincipalKind,
    PrincipalReference,
    PrincipalStatus,
    RegisterPrincipal,
    RegisterPrincipalCommand,
)
from lda.modules.identity.testing import InMemoryIdentityUnitOfWorkFactory
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import ConflictError, NotFoundError, ValidationError
from lda.shared.kernel.ids import PrincipalId

COMMAND = RegisterPrincipalCommand(kind=PrincipalKind.USER, display_name="  Ada Lovelace ")


class Wired:
    def __init__(
        self,
        factory: InMemoryIdentityUnitOfWorkFactory,
        clock: FakeClock,
        ids: SequentialIdGenerator,
    ) -> None:
        self.factory = factory
        self.clock = clock
        self.register = RegisterPrincipal(factory, clock, ids)
        self.disable = DisablePrincipal(factory, clock)
        self.enable = EnablePrincipal(factory, clock)
        self.get = GetPrincipal(factory)


@pytest.fixture
def wired(
    memory_factory: InMemoryIdentityUnitOfWorkFactory, clock: FakeClock, ids: SequentialIdGenerator
) -> Wired:
    return Wired(memory_factory, clock, ids)


def test_register_creates_an_active_principal_in_the_callers_tenant(
    wired: Wired, ctx_a: ExecutionContext
) -> None:
    view = wired.register.execute(ctx_a, COMMAND)
    assert view.tenant_id == ctx_a.tenant_id
    assert (view.kind, view.status, view.is_active) == (
        PrincipalKind.USER,
        PrincipalStatus.ACTIVE,
        True,
    )
    assert view.display_name == "Ada Lovelace"
    assert wired.get.execute(ctx_a, PrincipalReference(view.principal_id)) == view


def test_register_works_for_system_context(wired: Wired, system_a: ExecutionContext) -> None:
    view = wired.register.execute(
        system_a, RegisterPrincipalCommand(PrincipalKind.SERVICE, "ingest-worker")
    )
    assert view.kind is PrincipalKind.SERVICE
    assert view.tenant_id == system_a.tenant_id


def test_register_is_not_idempotent_each_call_mints_a_new_principal(
    wired: Wired, ctx_a: ExecutionContext
) -> None:
    first = wired.register.execute(ctx_a, COMMAND)
    second = wired.register.execute(ctx_a, COMMAND)
    assert first.principal_id != second.principal_id


@pytest.mark.parametrize("bad", ["", "   ", "a\nb", "x" * 201])
def test_register_rejects_invalid_display_names_without_touching_storage(
    wired: Wired, ctx_a: ExecutionContext, bad: str
) -> None:
    with pytest.raises(ValidationError) as info:
        wired.register.execute(ctx_a, RegisterPrincipalCommand(PrincipalKind.USER, bad))
    assert info.value.code == "display_name_invalid"
    assert info.value.detail == ""
    assert wired.factory.begun == []
    assert wired.factory.store.rows == {}


def test_register_commits_exactly_once(wired: Wired, ctx_a: ExecutionContext) -> None:
    wired.register.execute(ctx_a, COMMAND)
    (uow,) = wired.factory.begun
    assert uow.committed and not uow.rolled_back


def test_register_failure_leaves_nothing_behind(
    wired: Wired, ctx_a: ExecutionContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_self: object, _principal: object) -> None:
        raise ConflictError("principal_exists")

    monkeypatch.setattr(
        "lda.modules.identity.testing.in_memory.InMemoryPrincipalRepository.add", boom
    )
    with pytest.raises(ConflictError):
        wired.register.execute(ctx_a, COMMAND)
    assert wired.factory.store.rows == {}
    assert not wired.factory.begun[0].committed


def test_disable_then_enable_round_trip(wired: Wired, ctx_a: ExecutionContext) -> None:
    created = wired.register.execute(ctx_a, COMMAND)
    ref = PrincipalReference(created.principal_id)
    wired.clock.advance(timedelta(minutes=1))
    off = wired.disable.execute(ctx_a, ref)
    assert off.status is PrincipalStatus.DISABLED
    assert off.status_changed_at == created.created_at + timedelta(minutes=1)
    assert wired.get.execute(ctx_a, ref).status is PrincipalStatus.DISABLED
    wired.clock.advance(timedelta(minutes=1))
    assert wired.enable.execute(ctx_a, ref).is_active


def test_disable_is_idempotent_and_does_not_rewrite_state(
    wired: Wired, ctx_a: ExecutionContext
) -> None:
    ref = PrincipalReference(wired.register.execute(ctx_a, COMMAND).principal_id)
    first = wired.disable.execute(ctx_a, ref)
    wired.clock.advance(timedelta(hours=1))
    uows_before = len(wired.factory.begun)
    again = wired.disable.execute(ctx_a, ref)
    assert again == first  # same status_changed_at: no second transition
    assert not wired.factory.begun[uows_before].committed  # nothing to commit


def test_enable_is_idempotent_on_an_active_principal(wired: Wired, ctx_a: ExecutionContext) -> None:
    created = wired.register.execute(ctx_a, COMMAND)
    wired.clock.advance(timedelta(hours=1))
    assert wired.enable.execute(ctx_a, PrincipalReference(created.principal_id)) == created


def test_unknown_principal_is_not_found(wired: Wired, ctx_a: ExecutionContext) -> None:
    ref = PrincipalReference(PrincipalId(UUID(int=0xDEAD)))
    for call in (wired.get.execute, wired.disable.execute, wired.enable.execute):
        with pytest.raises(NotFoundError) as info:
            call(ctx_a, ref)
        assert info.value.code == "principal_not_found"


def test_tenants_are_isolated_and_cross_tenant_ids_look_absent(
    wired: Wired, ctx_a: ExecutionContext, ctx_b: ExecutionContext
) -> None:
    ref = PrincipalReference(wired.register.execute(ctx_a, COMMAND).principal_id)
    for call in (wired.get.execute, wired.disable.execute, wired.enable.execute):
        with pytest.raises(NotFoundError) as info:
            call(ctx_b, ref)
        assert info.value.code == "principal_not_found"
    assert wired.get.execute(ctx_a, ref).is_active  # tenant A is untouched


def test_concurrent_change_surfaces_as_conflict(wired: Wired, ctx_a: ExecutionContext) -> None:
    ref = PrincipalReference(wired.register.execute(ctx_a, COMMAND).principal_id)
    stale = wired.factory.begin(ctx_a)
    seen = stale.principals.get(ref.principal_id)
    assert seen is not None
    wired.disable.execute(ctx_a, ref)  # another request wins
    with pytest.raises(ConflictError) as info:
        stale.principals.save(seen.disable(wired.clock.now()))
    assert info.value.code == "principal_state_changed"


def test_use_cases_never_log(
    wired: Wired, ctx_a: ExecutionContext, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        view = wired.register.execute(
            ctx_a, RegisterPrincipalCommand(PrincipalKind.USER, "Jane Q. Confidential")
        )
        ref = PrincipalReference(view.principal_id)
        wired.disable.execute(ctx_a, ref)
        wired.enable.execute(ctx_a, ref)
        wired.get.execute(ctx_a, ref)
    assert caplog.records == []


def test_view_repr_does_not_contain_the_display_name(wired: Wired, ctx_a: ExecutionContext) -> None:
    view = wired.register.execute(
        ctx_a, RegisterPrincipalCommand(PrincipalKind.USER, "Jane Q. Confidential")
    )
    assert "Confidential" not in repr(view) + str(view)
    assert view.display_name == "Jane Q. Confidential"  # explicit access still works
