"""ADR-019: each effective Identity mutation emits exactly one fact; no-ops and failures emit none.

The in-memory unit of work records the facts handed to `uow.audit.record` in `audit.facts`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.identity.api import (
    DisablePrincipal,
    EnablePrincipal,
    PrincipalKind,
    PrincipalReference,
    RegisterPrincipal,
    RegisterPrincipalCommand,
)
from lda.modules.identity.testing import InMemoryIdentityUnitOfWorkFactory
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import NotFoundError
from lda.shared.kernel.ids import PrincipalId, RequestId
from lda.shared.ports.audit import AuditFact, AuditOutcome

REGISTER = RegisterPrincipalCommand(kind=PrincipalKind.USER, display_name="Ada Lovelace")


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

    def facts(self) -> list[AuditFact]:
        return [f for uow in self.factory.begun for f in uow.audit.facts]


@pytest.fixture
def wired(
    memory_factory: InMemoryIdentityUnitOfWorkFactory, clock: FakeClock, ids: SequentialIdGenerator
) -> Wired:
    return Wired(memory_factory, clock, ids)


def test_register_emits_one_success_fact_about_the_new_principal(
    wired: Wired, ctx_a: ExecutionContext
) -> None:
    view = wired.register.execute(ctx_a, REGISTER)
    (fact,) = wired.facts()
    assert fact == AuditFact(
        tenant_id=ctx_a.tenant_id,
        actor=PrincipalId(UUID(int=0xAC)),
        action="identity.principal_registered",
        resource_type="principal",
        resource_id=str(view.principal_id),
        outcome=AuditOutcome.SUCCESS,
        request_id=RequestId(UUID(int=1)),
        occurred_at=wired.clock.now(),
        attributes={"principal_kind": "user"},
    )


def test_register_by_system_work_has_no_actor_and_records_the_purpose(
    wired: Wired, system_a: ExecutionContext
) -> None:
    wired.register.execute(system_a, REGISTER)
    (fact,) = wired.facts()
    assert (fact.actor, fact.request_id) == (None, None)
    assert dict(fact.attributes) == {
        "purpose": "bootstrap-first-principal",
        "principal_kind": "user",
    }


def test_the_display_name_never_reaches_audit(wired: Wired, ctx_a: ExecutionContext) -> None:
    wired.register.execute(ctx_a, REGISTER)
    (fact,) = wired.facts()
    assert "Ada" not in repr(fact) and "Lovelace" not in repr(fact)


def test_a_failed_register_emits_nothing(wired: Wired, ctx_a: ExecutionContext) -> None:
    with pytest.raises(Exception):  # noqa: B017 - any failure, here an invalid name
        wired.register.execute(ctx_a, RegisterPrincipalCommand(PrincipalKind.USER, ""))
    assert wired.facts() == []


def test_disable_emits_one_fact_and_a_repeat_emits_none(
    wired: Wired, ctx_a: ExecutionContext
) -> None:
    pid = wired.register.execute(ctx_a, REGISTER).principal_id
    before = len(wired.facts())
    wired.clock.advance(timedelta(seconds=5))
    wired.disable.execute(ctx_a, PrincipalReference(pid))
    (fact,) = wired.facts()[before:]
    assert (fact.action, fact.resource_type, fact.resource_id) == (
        "identity.principal_disabled",
        "principal",
        str(pid),
    )
    assert (fact.outcome, fact.occurred_at, fact.actor) == (
        AuditOutcome.SUCCESS,
        wired.clock.now(),
        PrincipalId(UUID(int=0xAC)),
    )
    wired.disable.execute(ctx_a, PrincipalReference(pid))  # idempotent no-op
    assert len(wired.facts()) == before + 1


def test_enable_emits_one_fact_and_a_repeat_emits_none(
    wired: Wired, ctx_a: ExecutionContext
) -> None:
    pid = wired.register.execute(ctx_a, REGISTER).principal_id
    wired.disable.execute(ctx_a, PrincipalReference(pid))
    before = len(wired.facts())
    wired.clock.advance(timedelta(seconds=5))
    wired.enable.execute(ctx_a, PrincipalReference(pid))
    (fact,) = wired.facts()[before:]
    assert fact.action == "identity.principal_enabled"
    assert (fact.resource_id, fact.occurred_at) == (str(pid), wired.clock.now())
    wired.enable.execute(ctx_a, PrincipalReference(pid))  # already active: no-op
    assert len(wired.facts()) == before + 1


def test_enabling_an_already_active_principal_emits_nothing(
    wired: Wired, ctx_a: ExecutionContext
) -> None:
    pid = wired.register.execute(ctx_a, REGISTER).principal_id
    before = len(wired.facts())
    wired.enable.execute(ctx_a, PrincipalReference(pid))
    assert len(wired.facts()) == before


@pytest.mark.parametrize("which", ["disable", "enable"])
def test_an_unknown_principal_emits_nothing(
    wired: Wired, ctx_a: ExecutionContext, which: str
) -> None:
    use_case = wired.disable if which == "disable" else wired.enable
    with pytest.raises(NotFoundError):
        use_case.execute(ctx_a, PrincipalReference(PrincipalId(UUID(int=0xDEAD))))
    assert wired.facts() == []


def test_a_fact_is_staged_before_the_commit_that_publishes_the_change(
    wired: Wired, ctx_a: ExecutionContext
) -> None:
    wired.register.execute(ctx_a, REGISTER)
    (uow,) = wired.factory.begun
    assert uow.committed and len(uow.audit.facts) == 1
