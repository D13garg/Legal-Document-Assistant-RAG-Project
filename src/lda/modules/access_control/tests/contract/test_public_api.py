"""A consumer can use Access Control through api/ + testing/ alone; nothing internal leaks."""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from uuid import UUID

import pytest

import lda.modules.access_control.api as api
from lda.modules.access_control.api import (
    AccessScope,
    ActionId,
    AuthorizationDecision,
    PolicyEpoch,
    ResourceRef,
)
from lda.modules.access_control.testing import an_access_scope
from lda.shared.kernel.ids import PrincipalId, TenantId

EXPECTED_SURFACE = {
    "MAX_SCOPE_RESOURCES",
    "PROOF_LIFETIME",
    "AccessScope",
    "ActionId",
    "AuthorizationDecision",
    "AuthorizationProof",
    "AuthorizationRequest",
    "AuthorizeAction",
    "DenyReason",
    "Effect",
    "EpochCheckedProofVerifier",
    "GrantAccess",
    "GrantAccessCommand",
    "PolicyChangeView",
    "PolicyEpoch",
    "PolicyReader",
    "ResolveAccessScope",
    "ResourceKindDefinition",
    "ResourceRef",
    "ResourceRegistry",
    "RevokeAccess",
    "RevokeAccessCommand",
    "SigningKey",
    "SigningKeyring",
}


def test_surface_is_exactly_the_approved_contract() -> None:
    assert set(api.__all__) == EXPECTED_SURFACE
    assert all(hasattr(api, name) for name in api.__all__)


def test_internal_types_do_not_leak_through_the_api() -> None:
    for internal in ("ProofClaims", "Grant", "GrantRepository", "issue_proof", "open_token"):
        assert not hasattr(api, internal)


def test_api_package_imports_nothing_from_infrastructure_or_third_parties() -> None:
    source = Path(api.__file__ or "").read_text(encoding="utf-8")
    modules = [n.module or "" for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom)]
    assert modules and all(m.startswith("lda.modules.access_control.") for m in modules)
    assert not [m for m in modules if ".infrastructure" in m or ".testing" in m]


def test_a_scope_is_frozen_and_exact() -> None:
    scope = an_access_scope(
        tenant_id=TenantId(UUID(int=1)),
        principal_id=PrincipalId(UUID(int=2)),
        resources=frozenset({ResourceRef("document", "d1")}),
    )
    assert isinstance(scope.resources, frozenset)
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.resources = frozenset()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.policy_epoch = None  # type: ignore[misc,assignment]
    assert not hasattr(scope, "__dict__")  # slots: no ad-hoc attributes either


def test_scope_fields_are_exactly_the_approved_four() -> None:
    names = {f.name for f in dataclasses.fields(AccessScope)}
    assert names == {"tenant_id", "principal_id", "policy_epoch", "resources"}


def test_a_scope_rejects_mutable_or_foreign_resource_collections() -> None:
    from lda.modules.access_control.domain.errors import InvalidScope

    tenant, principal = TenantId(UUID(int=1)), PrincipalId(UUID(int=2))
    mutable: set[ResourceRef] = {ResourceRef("document", "d1")}
    foreign: frozenset[str] = frozenset({"d1"})
    count = api.MAX_SCOPE_RESOURCES + 1
    too_many = frozenset(ResourceRef("document", f"d{i}") for i in range(count))
    for bad in (mutable, foreign):
        with pytest.raises(InvalidScope):
            AccessScope(tenant, principal, PolicyEpoch(0), bad)  # type: ignore[arg-type]
    with pytest.raises(InvalidScope):
        an_access_scope(tenant_id=tenant, principal_id=principal, resources=too_many)


def test_decisions_cannot_be_inconsistent() -> None:
    from lda.modules.access_control.domain.decision import DenyReason, Effect
    from lda.modules.access_control.domain.errors import InvalidDecision

    action = ActionId("documents.read_document")
    with pytest.raises(InvalidDecision):  # ALLOW with nothing
        AuthorizationDecision(Effect.ALLOW, action, None, None, None)
    with pytest.raises(InvalidDecision):  # DENY carrying no reason
        AuthorizationDecision(Effect.DENY, action, None, None, None)
    ok = AuthorizationDecision.deny(action, DenyReason.NOT_GRANTED)
    assert not ok.allowed and ok.scope is None and ok.proof is None


def test_a_denial_can_never_carry_a_scope_or_a_proof() -> None:
    from lda.modules.access_control.domain.decision import DenyReason, Effect
    from lda.modules.access_control.domain.errors import InvalidDecision
    from lda.modules.access_control.domain.proof import issue_proof
    from lda.modules.access_control.testing import a_signing_keyring

    action = ActionId("documents.read_document")
    scope = an_access_scope(
        tenant_id=TenantId(UUID(int=1)),
        principal_id=PrincipalId(UUID(int=2)),
        resources=frozenset({ResourceRef("document", "d1")}),
    )
    from datetime import UTC, datetime

    proof = issue_proof(scope, action, datetime(2026, 1, 1, tzinfo=UTC), a_signing_keyring())
    reason = DenyReason.NOT_GRANTED
    with pytest.raises(InvalidDecision):
        AuthorizationDecision(Effect.DENY, action, scope, None, reason)
    with pytest.raises(InvalidDecision):
        AuthorizationDecision(Effect.DENY, action, None, proof, reason)
    with pytest.raises(InvalidDecision):
        AuthorizationDecision(Effect.ALLOW, action, scope, proof, reason)  # allow with a reason
    with pytest.raises(InvalidDecision):
        AuthorizationDecision(Effect.ALLOW, action, scope, None, None)  # allow without a proof
    with pytest.raises(InvalidDecision):
        AuthorizationDecision(Effect.ALLOW, action, None, proof, None)  # allow without a scope
    empty = an_access_scope(
        tenant_id=TenantId(UUID(int=1)),
        principal_id=PrincipalId(UUID(int=2)),
        resources=frozenset(),
    )
    with pytest.raises(InvalidDecision):  # allow with an empty scope
        AuthorizationDecision(Effect.ALLOW, action, empty, proof, None)
