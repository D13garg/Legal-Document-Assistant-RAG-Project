"""Default deny, explicit grants, revocation, epochs, identity boundary, tenant isolation.

Every test runs on the in-memory AND the Postgres-backed policy storage (`any_rig`).
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.access_control.api import (
    ActionId,
    AuthorizationDecision,
    AuthorizationRequest,
    DenyReason,
    GrantAccessCommand,
    PolicyChangeView,
    ResourceRef,
    RevokeAccessCommand,
)
from lda.modules.access_control.tests.support import (
    ALICE,
    BOB,
    CAROL,
    TENANT_A,
    TENANT_B,
    Rig,
    ctx_for,
    disable_principal,
)
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import AuthorizationError, NotFoundError, ValidationError
from lda.shared.kernel.ids import PrincipalId, TenantId

READ = ActionId("documents.read_document")
DELETE = ActionId("documents.delete_document")
D1, D2, D3 = (ResourceRef("document", n) for n in ("d1", "d2", "d3"))


def actor(tenant: TenantId) -> PrincipalId:
    return ALICE if tenant == TENANT_A else CAROL


def grant(
    rig: Rig,
    resource: ResourceRef,
    action: ActionId = READ,
    *,
    tenant: TenantId = TENANT_A,
    to: PrincipalId | None = None,
) -> PolicyChangeView:
    command = GrantAccessCommand(to or actor(tenant), resource, action)
    return rig.grant.execute(ctx_for(tenant, actor(tenant)), command)


def revoke(
    rig: Rig,
    resource: ResourceRef,
    action: ActionId = READ,
    *,
    tenant: TenantId = TENANT_A,
    of: PrincipalId | None = None,
) -> PolicyChangeView:
    command = RevokeAccessCommand(of or actor(tenant), resource, action)
    return rig.revoke.execute(ctx_for(tenant, actor(tenant)), command)


def ask(
    rig: Rig,
    *resources: ResourceRef,
    action: ActionId = READ,
    tenant: TenantId = TENANT_A,
    who: PrincipalId | None = None,
) -> AuthorizationDecision:
    ctx = ctx_for(tenant, who or actor(tenant))
    return rig.authorize.execute(ctx, AuthorizationRequest(action, frozenset(resources)))


def resolve(
    rig: Rig,
    *resources: ResourceRef,
    action: ActionId = READ,
    tenant: TenantId = TENANT_A,
    who: PrincipalId | None = None,
) -> AuthorizationDecision:
    ctx = ctx_for(tenant, who or actor(tenant))
    return rig.resolve.execute(ctx, AuthorizationRequest(action, frozenset(resources)))


# --- 1. default deny ---
def test_default_deny_without_any_grant(any_rig: Rig) -> None:
    decision = ask(any_rig, D1)
    assert not decision.allowed
    assert (decision.reason, decision.scope, decision.proof) == (DenyReason.NOT_GRANTED, None, None)
    assert not resolve(any_rig, D1, D2).allowed


# --- 2. explicit grant allows ---
def test_an_explicit_grant_allows_exactly_that_resource_and_action(any_rig: Rig) -> None:
    grant(any_rig, D1)
    decision = ask(any_rig, D1)
    assert decision.allowed and decision.scope is not None and decision.proof is not None
    assert decision.scope.resources == frozenset({D1})
    assert (decision.scope.tenant_id, decision.scope.principal_id) == (TENANT_A, ALICE)
    assert not ask(any_rig, D2).allowed  # another resource
    assert not ask(any_rig, D1, action=DELETE).allowed  # another action


def test_grants_are_per_principal(any_rig: Rig) -> None:
    grant(any_rig, D1, to=ALICE)
    assert ask(any_rig, D1, who=ALICE).allowed
    assert not ask(any_rig, D1, who=BOB).allowed


# --- strict versus filtering decisions ---
def test_authorize_is_all_or_nothing(any_rig: Rig) -> None:
    grant(any_rig, D1)
    decision = ask(any_rig, D1, D2)
    assert not decision.allowed and decision.reason is DenyReason.NOT_GRANTED
    assert decision.scope is None and decision.proof is None


def test_resolve_returns_exactly_the_granted_subset(any_rig: Rig) -> None:
    grant(any_rig, D1)
    grant(any_rig, D3)
    decision = resolve(any_rig, D1, D2, D3)
    assert decision.scope is not None
    assert decision.scope.resources == frozenset({D1, D3})  # D2 is never present


def test_scope_is_never_wider_than_the_request(any_rig: Rig) -> None:
    for resource in (D1, D2, D3):
        grant(any_rig, resource)
    decision = resolve(any_rig, D2)
    assert decision.scope is not None and decision.scope.resources == frozenset({D2})


# --- 3. revoke denies, 4. epoch advances ---
def test_revocation_denies_again(any_rig: Rig) -> None:
    grant(any_rig, D1)
    assert ask(any_rig, D1).allowed
    assert revoke(any_rig, D1).changed
    assert not ask(any_rig, D1).allowed


def test_epoch_advances_by_one_per_changing_operation(any_rig: Rig) -> None:
    assert ask(any_rig, D1).reason is DenyReason.NOT_GRANTED  # epoch 0, no row yet
    assert grant(any_rig, D1).policy_epoch == 1
    assert grant(any_rig, D2).policy_epoch == 2
    assert revoke(any_rig, D1).policy_epoch == 3
    decision = ask(any_rig, D2)
    assert decision.scope is not None and decision.scope.policy_epoch.value == 3


def test_idempotent_operations_do_not_advance_the_epoch(any_rig: Rig) -> None:
    assert grant(any_rig, D1).changed
    again = grant(any_rig, D1)
    assert (again.changed, again.policy_epoch) == (False, 1)
    missing = revoke(any_rig, D2)
    assert (missing.changed, missing.policy_epoch) == (False, 1)


def test_epoch_is_per_tenant(any_rig: Rig) -> None:
    grant(any_rig, D1)
    grant(any_rig, D2)
    assert grant(any_rig, D1, tenant=TENANT_B).policy_epoch == 1
    assert grant(any_rig, D1).policy_epoch == 2  # idempotent in A: unchanged by B's activity


# --- 12. cross-tenant ---
def test_a_grant_in_one_tenant_gives_nothing_in_another(any_rig: Rig) -> None:
    grant(any_rig, D1, tenant=TENANT_A)
    assert ask(any_rig, D1, tenant=TENANT_A).allowed
    assert not ask(any_rig, D1, tenant=TENANT_B).allowed  # same principal id, same resource id
    assert not resolve(any_rig, D1, tenant=TENANT_B).allowed


def test_revocation_in_one_tenant_does_not_touch_another(any_rig: Rig) -> None:
    grant(any_rig, D1, tenant=TENANT_A)
    grant(any_rig, D1, tenant=TENANT_B)
    assert revoke(any_rig, D1, tenant=TENANT_A).changed
    assert ask(any_rig, D1, tenant=TENANT_B).allowed


def test_granting_to_another_tenants_principal_looks_like_an_absent_principal(any_rig: Rig) -> None:
    other = PrincipalId(UUID(int=0xDEAD))
    with pytest.raises(NotFoundError) as caught:
        grant(any_rig, D1, to=other)
    assert caught.value.code == "principal_not_found"


# --- 11. identity boundary: live status, never cached ---
def test_a_disabled_principal_is_denied_immediately(any_rig: Rig) -> None:
    grant(any_rig, D1)
    assert ask(any_rig, D1).allowed
    disable_principal(any_rig, TENANT_A, ALICE)
    decision = ask(any_rig, D1)
    assert not decision.allowed and decision.reason is DenyReason.PRINCIPAL_NOT_ACTIVE
    assert not resolve(any_rig, D1).allowed


def test_status_is_not_cached_across_decisions(any_rig: Rig) -> None:
    grant(any_rig, D1)
    disable_principal(any_rig, TENANT_A, ALICE)
    assert not ask(any_rig, D1).allowed
    from lda.modules.identity.api import EnablePrincipal, PrincipalReference

    EnablePrincipal(any_rig.identity, any_rig.clock).execute(
        ctx_for(TENANT_A, ALICE), PrincipalReference(ALICE)
    )
    assert ask(any_rig, D1).allowed  # live again, no restart needed


def test_an_unknown_principal_is_denied_not_errored(any_rig: Rig) -> None:
    ghost = PrincipalId(UUID(int=0xDEAD))
    decision = ask(any_rig, D1, who=ghost)
    assert decision.reason is DenyReason.PRINCIPAL_NOT_ACTIVE


def test_a_principal_of_another_tenant_cannot_ask_here(any_rig: Rig) -> None:
    # BOB exists only in tenant A; presented in tenant B he is absent there.
    assert ask(any_rig, D1, tenant=TENANT_B, who=BOB).reason is DenyReason.PRINCIPAL_NOT_ACTIVE


def test_a_system_context_cannot_obtain_a_decision(any_rig: Rig) -> None:
    system = SystemContext(TENANT_A, "job")
    request = AuthorizationRequest(READ, frozenset({D1}))
    for use_case in (any_rig.authorize, any_rig.resolve):
        with pytest.raises(AuthorizationError) as caught:
            use_case.execute(system, request)
        assert caught.value.code == "authorization_requires_principal"


# --- registry ---
def test_unregistered_actions_and_kinds_cannot_be_granted(any_rig: Rig) -> None:
    with pytest.raises(ValidationError):
        grant(any_rig, D1, ActionId("documents.purge_document"))
    with pytest.raises(ValidationError):
        grant(any_rig, ResourceRef("folder", "f1"))
    assert grant(any_rig, D1).changed  # the registered pair still works


def test_unregistered_pairs_are_unknown_action_on_decision(any_rig: Rig) -> None:
    assert ask(any_rig, ResourceRef("folder", "f1")).reason is DenyReason.UNKNOWN_ACTION
    assert ask(any_rig, D1, action=ActionId("documents.purge_document")).reason is (
        DenyReason.UNKNOWN_ACTION
    )
    mixed = ask(any_rig, D1, ResourceRef("folder", "f1"))
    assert mixed.reason is DenyReason.UNKNOWN_ACTION


def test_a_stale_grant_for_a_removed_registration_is_inert_and_revocable(any_rig: Rig) -> None:
    from lda.modules.access_control.api import ResourceKindDefinition
    from lda.modules.access_control.domain.registry import ResourceRegistry

    # Grant while "document" is registered...
    grant(any_rig, D1)
    # ...then a decision rig whose registry no longer knows the kind: nothing is honoured.
    from lda.modules.access_control.api import AuthorizeAction, PolicyReader
    from lda.modules.identity.api import GetPrincipal

    narrowed = ResourceRegistry()
    narrowed.register(ResourceKindDefinition("folder", frozenset({READ})))
    narrowed.seal()
    reader = PolicyReader(any_rig.policy, GetPrincipal(any_rig.identity), narrowed)
    strict = AuthorizeAction(reader, any_rig.clock, any_rig.keyring)
    decision = strict.execute(ctx_for(TENANT_A, ALICE), AuthorizationRequest(READ, frozenset({D1})))
    assert decision.reason is DenyReason.UNKNOWN_ACTION
    assert revoke(any_rig, D1).changed  # revocation does not depend on the registry


# --- request validation ---
def test_requests_must_name_resources() -> None:
    with pytest.raises(ValidationError):
        AuthorizationRequest(READ, frozenset())
    with pytest.raises(ValidationError):
        AuthorizationRequest(READ, [D1])  # type: ignore[arg-type]


def test_oversized_requests_are_refused() -> None:
    too_many = frozenset(ResourceRef("document", f"d{i}") for i in range(10_001))
    with pytest.raises(ValidationError):
        AuthorizationRequest(READ, too_many)


def test_a_decision_is_stable_within_one_clock_tick(any_rig: Rig) -> None:
    grant(any_rig, D1)
    first, second = ask(any_rig, D1), ask(any_rig, D1)
    assert first.proof is not None and second.proof is not None
    assert first.proof.token == second.proof.token  # deterministic HMAC over identical claims
    any_rig.clock.advance(timedelta(seconds=1))
    third = ask(any_rig, D1)
    assert third.proof is not None and third.proof.token != first.proof.token


class _Spy:
    """Wraps a repository and appends the name of each read to a shared log."""

    def __init__(self, inner: object, name: str, log: list[str]) -> None:
        self._inner, self._name, self._log = inner, name, log

    def __getattr__(self, attribute: str) -> object:
        target = getattr(self._inner, attribute)
        if attribute not in {"current", "granted"}:
            return target

        def recorded(*args: object, **kwargs: object) -> object:
            self._log.append(f"{self._name}.{attribute}")
            return target(*args, **kwargs)

        return recorded


def test_the_epoch_is_read_before_the_grants(any_rig: Rig) -> None:
    """ADR-014 section 8: a concurrent change can then only make a decision look OLDER."""
    from lda.modules.access_control.api import AuthorizeAction, PolicyReader
    from lda.modules.identity.api import GetPrincipal

    log: list[str] = []

    class SpyUnitOfWork:
        def __init__(self, inner: object) -> None:
            self._inner = inner
            self.grants = _Spy(inner.grants, "grants", log)  # type: ignore[attr-defined]
            self.policy_epochs = _Spy(inner.policy_epochs, "epoch", log)  # type: ignore[attr-defined]

        def __enter__(self) -> SpyUnitOfWork:
            self._inner.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *exc: object) -> None:
            self._inner.__exit__(*exc)  # type: ignore[attr-defined]

    class SpyFactory:
        def begin(self, ctx: object) -> SpyUnitOfWork:
            return SpyUnitOfWork(any_rig.policy.begin(ctx))  # type: ignore[arg-type]

    grant(any_rig, D1)
    reader = PolicyReader(SpyFactory(), GetPrincipal(any_rig.identity), any_rig.registry)  # type: ignore[arg-type]
    decision = AuthorizeAction(reader, any_rig.clock, any_rig.keyring).execute(
        ctx_for(TENANT_A, ALICE), AuthorizationRequest(READ, frozenset({D1}))
    )
    assert decision.allowed
    assert log == ["epoch.current", "grants.granted"]
