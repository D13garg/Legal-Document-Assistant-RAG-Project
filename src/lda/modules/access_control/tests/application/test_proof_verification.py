"""The egress check: integrity, exact binding, expiry and the LIVE policy epoch (ADR-026).

Every test runs on the in-memory AND the Postgres-backed policy storage (`any_rig`).
"""

from __future__ import annotations

from datetime import timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

from lda.modules.access_control.api import (
    ActionId,
    AuthorizationProof,
    AuthorizationRequest,
    EpochCheckedProofVerifier,
    GrantAccessCommand,
    ResourceRef,
    RevokeAccessCommand,
)
from lda.modules.access_control.tests.support import (
    ALICE,
    BOB,
    TENANT_A,
    TENANT_B,
    Rig,
    ctx_for,
    disable_principal,
)
from lda.modules.identity.api import EnablePrincipal, GetPrincipal, PrincipalReference
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import AuthorizationError, InfrastructureError
from lda.shared.kernel.ids import PrincipalId, TenantId
from lda.shared.ports.authorization import ProofExpectation, ProofResource, ProofVerifier

READ = ActionId("documents.read_document")
DELETE = ActionId("documents.delete_document")
D1, D2, D3 = (ResourceRef("document", n) for n in ("d1", "d2", "d3"))


def seed(rig: Rig, *resources: ResourceRef, action: ActionId = READ) -> None:
    for resource in resources:
        rig.grant.execute(ctx_for(TENANT_A, ALICE), GrantAccessCommand(ALICE, resource, action))


def mint(rig: Rig, *resources: ResourceRef, action: ActionId = READ) -> AuthorizationProof:
    decision = rig.authorize.execute(
        ctx_for(TENANT_A, ALICE), AuthorizationRequest(action, frozenset(resources))
    )
    assert decision.proof is not None
    return decision.proof


def expectation(
    *resources: ResourceRef,
    tenant: TenantId = TENANT_A,
    principal: PrincipalId = ALICE,
    action: ActionId = READ,
) -> ProofExpectation:
    return ProofExpectation(
        tenant_id=tenant,
        principal_id=principal,
        action=action.value,
        resources=frozenset(ProofResource(r.kind, r.id) for r in resources),
    )


def rejected(rig: Rig, token: str, expected: ProofExpectation) -> str:
    with pytest.raises(AuthorizationError) as caught:
        rig.verifier.verify(token, expected)
    return caught.value.code


def test_the_concrete_verifier_satisfies_the_shared_port(any_rig: Rig) -> None:
    verifier: ProofVerifier = any_rig.verifier
    seed(any_rig, D1)
    verifier.verify(mint(any_rig, D1).token, expectation(D1))  # returns, nothing raised


def test_a_fresh_proof_for_exactly_the_authorized_set_verifies(any_rig: Rig) -> None:
    seed(any_rig, D1, D2)
    proof = mint(any_rig, D1, D2)
    any_rig.verifier.verify(proof.token, expectation(D1, D2))


# --- 5. stale epoch ---
def test_a_correctly_signed_proof_with_a_stale_epoch_is_rejected(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    any_rig.verifier.verify(proof.token, expectation(D1))
    seed(any_rig, D2)  # ANY policy change in the tenant moves the epoch
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_stale_epoch"


def test_revocation_invalidates_outstanding_proofs_immediately(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    any_rig.revoke.execute(ctx_for(TENANT_A, ALICE), RevokeAccessCommand(ALICE, D1, READ))
    # Well inside the 60-second lifetime, yet no longer verifiable.
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_stale_epoch"


def test_an_idempotent_grant_does_not_invalidate_proofs(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    seed(any_rig, D1)  # no change, no epoch move
    any_rig.verifier.verify(proof.token, expectation(D1))


def test_another_tenants_policy_change_does_not_invalidate_this_tenants_proof(
    any_rig: Rig,
) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    from lda.modules.access_control.tests.support import CAROL

    any_rig.grant.execute(ctx_for(TENANT_B, CAROL), GrantAccessCommand(CAROL, D1, READ))
    any_rig.verifier.verify(proof.token, expectation(D1))


# --- 6. modified proof ---
def test_a_modified_token_is_rejected(any_rig: Rig) -> None:
    seed(any_rig, D1)
    version, kid, payload, mac = mint(any_rig, D1).token.split(".")
    tampered = ".".join(
        [version, kid, payload[:-2] + ("AA" if payload[-2:] != "AA" else "BB"), mac]
    )
    assert rejected(any_rig, tampered, expectation(D1)) in {
        "proof_signature_invalid",
        "proof_malformed",
    }
    forged_mac = ".".join([version, kid, payload, mac[:-2] + ("AA" if mac[-2:] != "AA" else "BB")])
    assert rejected(any_rig, forged_mac, expectation(D1)) == "proof_signature_invalid"


def test_a_proof_signed_with_another_key_is_rejected(any_rig: Rig) -> None:
    from lda.modules.access_control.domain.proof import issue_proof
    from lda.modules.access_control.testing import a_signing_keyring, an_access_scope

    seed(any_rig, D1)
    scope = an_access_scope(
        tenant_id=TENANT_A, principal_id=ALICE, resources=frozenset({D1}), policy_epoch=1
    )
    other = a_signing_keyring(secret=b"an-attackers-own-secret-value-0000")
    forged = issue_proof(scope, READ, any_rig.clock.now(), other)
    assert rejected(any_rig, forged.token, expectation(D1)) == "proof_signature_invalid"


# --- 7, 8, 9. wrong tenant / principal / resource set (and action) ---
def test_wrong_tenant_is_rejected(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    assert rejected(any_rig, proof.token, expectation(D1, tenant=TENANT_B)) == "proof_mismatch"


def test_wrong_principal_is_rejected(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    assert rejected(any_rig, proof.token, expectation(D1, principal=BOB)) == "proof_mismatch"


def test_wrong_action_is_rejected(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    assert rejected(any_rig, proof.token, expectation(D1, action=DELETE)) == "proof_mismatch"


@pytest.mark.parametrize(
    "presented",
    [
        pytest.param((D1, D2), id="superset"),
        pytest.param((D3,), id="different resource"),
        pytest.param((D2, D3), id="disjoint pair"),
        pytest.param((ResourceRef("folder", "d1"),), id="same id, different kind"),
    ],
)
def test_wrong_resource_set_is_rejected(any_rig: Rig, presented: tuple[ResourceRef, ...]) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)  # authorizes exactly {D1}
    assert rejected(any_rig, proof.token, expectation(*presented)) == "proof_mismatch"


def test_a_subset_of_the_authorized_set_is_rejected(any_rig: Rig) -> None:
    seed(any_rig, D1, D2)
    proof = mint(any_rig, D1, D2)
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_mismatch"


def test_an_empty_resource_set_is_rejected(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    assert rejected(any_rig, proof.token, expectation()) == "proof_mismatch"


# --- 10. expiry ---
def test_a_proof_is_valid_until_sixty_seconds_and_not_after(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    expected = expectation(D1)
    any_rig.clock.advance(timedelta(seconds=59))
    any_rig.verifier.verify(proof.token, expected)
    any_rig.clock.advance(timedelta(seconds=1))  # exactly issued_at + 60 s
    assert rejected(any_rig, proof.token, expected) == "proof_expired"
    any_rig.clock.advance(timedelta(hours=1))
    assert rejected(any_rig, proof.token, expected) == "proof_expired"


def test_expiry_is_checked_even_when_the_epoch_is_current(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    any_rig.clock.advance(timedelta(seconds=61))
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_expired"


# --- 11. disabled principal ---
def test_a_new_decision_for_a_disabled_principal_yields_no_proof(any_rig: Rig) -> None:
    seed(any_rig, D1)
    disable_principal(any_rig, TENANT_A, ALICE)
    decision = any_rig.authorize.execute(
        ctx_for(TENANT_A, ALICE), AuthorizationRequest(READ, frozenset({D1}))
    )
    assert decision.proof is None and not decision.allowed


def test_a_proof_minted_while_active_is_rejected_immediately_after_disable(any_rig: Rig) -> None:
    """ADR-026 §7(e): the principal is checked live; no waiting for expiry, no epoch change."""
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    any_rig.verifier.verify(proof.token, expectation(D1))  # valid while active
    disable_principal(any_rig, TENANT_A, ALICE)
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_principal_inactive"
    any_rig.clock.advance(timedelta(seconds=1))  # still well inside the 60 s lifetime
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_principal_inactive"


def test_disabling_does_not_touch_the_policy_epoch(any_rig: Rig) -> None:
    """Identity never bumps the epoch (owner decision): only grant changes move it."""
    seed(any_rig, D1)
    before = mint(any_rig, D1).claims.policy_epoch
    disable_principal(any_rig, TENANT_A, ALICE)
    with any_rig.policy.begin(ctx_for(TENANT_A, ALICE)) as uow:
        assert uow.policy_epochs.current() == before


def test_other_principals_proofs_are_unaffected_by_a_disable(any_rig: Rig) -> None:
    seed(any_rig, D1)
    any_rig.grant.execute(ctx_for(TENANT_A, ALICE), GrantAccessCommand(BOB, D1, READ))
    bobs = any_rig.authorize.execute(
        ctx_for(TENANT_A, BOB), AuthorizationRequest(READ, frozenset({D1}))
    )
    assert bobs.proof is not None
    disable_principal(any_rig, TENANT_A, ALICE)
    any_rig.verifier.verify(bobs.proof.token, expectation(D1, principal=BOB))


def enable_principal(rig: Rig, tenant: TenantId, principal: PrincipalId) -> None:
    EnablePrincipal(rig.identity, rig.clock).execute(
        ctx_for(tenant, principal), PrincipalReference(principal)
    )


def test_reenabling_does_not_revive_an_expired_proof(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    disable_principal(any_rig, TENANT_A, ALICE)
    any_rig.clock.advance(timedelta(seconds=60))
    enable_principal(any_rig, TENANT_A, ALICE)
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_expired"


def test_reenabling_does_not_revive_a_stale_epoch_proof(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    disable_principal(any_rig, TENANT_A, ALICE)
    seed(any_rig, D2)  # a policy change while disabled moves the epoch
    enable_principal(any_rig, TENANT_A, ALICE)
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_stale_epoch"


def test_reenabling_does_not_revive_a_tampered_or_mismatched_proof(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    disable_principal(any_rig, TENANT_A, ALICE)
    enable_principal(any_rig, TENANT_A, ALICE)
    version, kid, payload, mac = proof.token.split(".")
    flipped = mac[:-2] + ("AA" if mac[-2:] != "AA" else "BB")
    assert rejected(any_rig, ".".join([version, kid, payload, flipped]), expectation(D1)) == (
        "proof_signature_invalid"
    )
    assert rejected(any_rig, proof.token, expectation(D2)) == "proof_mismatch"
    assert rejected(any_rig, proof.token, expectation(D1, principal=BOB)) == "proof_mismatch"


def test_a_proof_that_satisfies_all_five_conditions_verifies_again_after_reenable(
    any_rig: Rig,
) -> None:
    """Validity is the conjunction of the five rules, evaluated live; nothing latches a denial."""
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    disable_principal(any_rig, TENANT_A, ALICE)
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_principal_inactive"
    enable_principal(any_rig, TENANT_A, ALICE)
    any_rig.verifier.verify(proof.token, expectation(D1))  # unexpired, current epoch, active


def test_an_absent_principal_is_inactive_even_with_a_validly_signed_proof(any_rig: Rig) -> None:
    from lda.modules.access_control.domain.proof import issue_proof
    from lda.modules.access_control.testing import an_access_scope

    ghost = PrincipalId(UUID(int=0xDEAD))
    scope = an_access_scope(tenant_id=TENANT_A, principal_id=ghost, resources=frozenset({D1}))
    forged = issue_proof(scope, READ, any_rig.clock.now(), any_rig.keyring)
    assert rejected(any_rig, forged.token, expectation(D1, principal=ghost)) == (
        "proof_principal_inactive"
    )


def test_a_principal_of_another_tenant_does_not_count_as_active(any_rig: Rig) -> None:
    """ALICE is active in tenant A; a proof claiming her in tenant B must not pass."""
    from lda.modules.access_control.domain.proof import issue_proof
    from lda.modules.access_control.testing import an_access_scope

    scope = an_access_scope(tenant_id=TENANT_B, principal_id=ALICE, resources=frozenset({D1}))
    forged = issue_proof(scope, READ, any_rig.clock.now(), any_rig.keyring)
    assert rejected(any_rig, forged.token, expectation(D1, tenant=TENANT_B)) == (
        "proof_principal_inactive"
    )


def test_verification_order_is_cheap_checks_then_epoch_then_principal(any_rig: Rig) -> None:
    """Precedence of reported codes for a proof failing several rules at once."""
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    disable_principal(any_rig, TENANT_A, ALICE)
    seed(any_rig, D2)  # stale AND principal disabled -> the epoch rule reports first
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_stale_epoch"
    assert rejected(any_rig, proof.token, expectation(D2)) == "proof_mismatch"
    any_rig.clock.advance(timedelta(seconds=60))  # expired as well -> expiry reports first
    assert rejected(any_rig, proof.token, expectation(D1)) == "proof_expired"


class _BrokenIdentityUnitOfWorkFactory:
    def begin(self, ctx: ExecutionContext) -> object:
        raise InfrastructureError("identity_persistence_failed")


def test_an_identity_failure_means_no_verification_result(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    broken = EpochCheckedProofVerifier(
        any_rig.keyring,
        any_rig.policy,
        GetPrincipal(_BrokenIdentityUnitOfWorkFactory()),  # type: ignore[arg-type]
        any_rig.clock,
    )
    with pytest.raises(InfrastructureError):
        broken.verify(proof.token, expectation(D1))


def test_cheap_and_epoch_failures_never_reach_identity(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    seed(any_rig, D2)  # stale
    probe = EpochCheckedProofVerifier(
        any_rig.keyring,
        any_rig.policy,
        GetPrincipal(_BrokenIdentityUnitOfWorkFactory()),  # type: ignore[arg-type]
        any_rig.clock,
    )
    with pytest.raises(AuthorizationError) as caught:  # would raise InfrastructureError if reached
        probe.verify(proof.token, expectation(D1))
    assert caught.value.code == "proof_stale_epoch"
    with pytest.raises(AuthorizationError):
        probe.verify("garbage", expectation(D1))


# --- tokens that never came from us ---
@pytest.mark.parametrize("token", ["", "garbage", "v1.a.b.c", "a.b.c.d", "v1..."])
def test_garbage_is_rejected(any_rig: Rig, token: str) -> None:
    assert rejected(any_rig, token, expectation(D1)).startswith("proof_")


def test_errors_do_not_carry_the_token_or_resource_ids(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    with pytest.raises(AuthorizationError) as caught:
        any_rig.verifier.verify(proof.token, expectation(D2))
    text = f"{caught.value!s} {caught.value.detail} {caught.value!r}"
    assert proof.token not in text and "d1" not in text and "d2" not in text


# --- fail closed ---
class _BrokenUnitOfWorkFactory:
    def begin(self, ctx: ExecutionContext) -> _BrokenUnitOfWork:
        return _BrokenUnitOfWork()


class _BrokenUnitOfWork:
    @property
    def policy_epochs(self) -> object:
        raise InfrastructureError("access_control_persistence_failed")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def test_a_database_failure_means_no_verification_result(any_rig: Rig) -> None:
    seed(any_rig, D1)
    proof = mint(any_rig, D1)
    broken = EpochCheckedProofVerifier(
        any_rig.keyring,
        _BrokenUnitOfWorkFactory(),  # type: ignore[arg-type]
        GetPrincipal(any_rig.identity),
        any_rig.clock,
    )
    with pytest.raises(InfrastructureError):
        broken.verify(proof.token, expectation(D1))


def test_cheap_failures_never_reach_the_database(any_rig: Rig) -> None:
    broken = EpochCheckedProofVerifier(
        any_rig.keyring,
        _BrokenUnitOfWorkFactory(),  # type: ignore[arg-type]
        GetPrincipal(any_rig.identity),
        any_rig.clock,
    )
    with pytest.raises(AuthorizationError):  # malformed: rejected before any read
        broken.verify("garbage", expectation(D1))
