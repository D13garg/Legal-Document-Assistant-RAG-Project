"""ADR-019: effective policy changes emit exactly one fact; no-ops, failures and reads emit none.

Runs on the in-memory policy storage, whose unit of work records the facts handed to
`uow.audit.record`. The Postgres-backed path is covered by `tests/platform/test_audit_bootstrap.py`.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.access_control.api import (
    ActionId,
    AuthorizationRequest,
    GrantAccessCommand,
    ResourceRef,
    RevokeAccessCommand,
)
from lda.modules.access_control.tests.support import ALICE, BOB, TENANT_A, Rig, ctx_for
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import NotFoundError, ValidationError
from lda.shared.kernel.ids import PrincipalId, RequestId
from lda.shared.ports.audit import AuditFact, AuditOutcome

READ = ActionId("documents.read_document")
D1 = ResourceRef("document", "d1")


def facts(rig: Rig) -> list[AuditFact]:
    return [f for uow in rig.policy.begun for f in uow.audit.facts]  # type: ignore[attr-defined]


def test_a_grant_emits_one_fact_naming_the_grant_by_identifiers_only(rig: Rig) -> None:
    ctx = ctx_for(TENANT_A, ALICE, 7)
    view = rig.grant.execute(ctx, GrantAccessCommand(BOB, D1, READ))
    (fact,) = facts(rig)
    assert fact == AuditFact(
        tenant_id=TENANT_A,
        actor=ALICE,
        action="access_control.access_granted",
        resource_type="document",
        resource_id="d1",
        outcome=AuditOutcome.SUCCESS,
        request_id=RequestId(UUID(int=7)),
        occurred_at=rig.clock.now(),
        attributes={
            "principal_id": str(BOB),
            "granted_action": "documents.read_document",
            "policy_epoch": str(view.policy_epoch),
        },
    )


def test_a_repeated_grant_changes_nothing_and_emits_nothing(rig: Rig) -> None:
    ctx = ctx_for(TENANT_A, ALICE)
    rig.grant.execute(ctx, GrantAccessCommand(BOB, D1, READ))
    again = rig.grant.execute(ctx, GrantAccessCommand(BOB, D1, READ))
    assert not again.changed
    assert len(facts(rig)) == 1


def test_a_revoke_emits_one_fact_and_records_the_new_epoch(rig: Rig) -> None:
    ctx = ctx_for(TENANT_A, ALICE, 8)
    rig.grant.execute(ctx, GrantAccessCommand(BOB, D1, READ))
    rig.clock.advance(timedelta(seconds=30))
    view = rig.revoke.execute(ctx, RevokeAccessCommand(BOB, D1, READ))
    assert view.changed
    revoked = facts(rig)[1:]
    assert len(revoked) == 1
    fact = revoked[0]
    assert (fact.action, fact.resource_type, fact.resource_id) == (
        "access_control.access_revoked",
        "document",
        "d1",
    )
    assert (fact.outcome, fact.actor, fact.occurred_at) == (
        AuditOutcome.SUCCESS,
        ALICE,
        rig.clock.now(),
    )
    assert dict(fact.attributes) == {
        "principal_id": str(BOB),
        "revoked_action": "documents.read_document",
        "policy_epoch": str(view.policy_epoch),
    }


def test_revoking_an_absent_grant_emits_nothing(rig: Rig) -> None:
    view = rig.revoke.execute(ctx_for(TENANT_A, ALICE), RevokeAccessCommand(BOB, D1, READ))
    assert not view.changed
    assert facts(rig) == []


def test_system_work_has_no_actor_and_records_its_purpose(rig: Rig) -> None:
    rig.grant.execute(
        SystemContext(TENANT_A, "seed-first-grant"), GrantAccessCommand(BOB, D1, READ)
    )
    (fact,) = facts(rig)
    assert (fact.actor, fact.request_id) == (None, None)
    assert fact.attributes["purpose"] == "seed-first-grant"


@pytest.mark.parametrize(
    "command",
    [
        GrantAccessCommand(PrincipalId(UUID(int=0xDEAD)), D1, READ),  # unknown principal
        GrantAccessCommand(BOB, ResourceRef("unregistered_kind", "x"), READ),  # unknown kind
        GrantAccessCommand(BOB, D1, ActionId("documents.not_an_action")),  # action not allowed
    ],
)
def test_a_rejected_grant_emits_nothing(rig: Rig, command: GrantAccessCommand) -> None:
    with pytest.raises((NotFoundError, ValidationError)):
        rig.grant.execute(ctx_for(TENANT_A, ALICE), command)
    assert facts(rig) == []


def test_decisions_are_not_audited_in_step_4(rig: Rig) -> None:
    ctx = ctx_for(TENANT_A, ALICE)
    rig.grant.execute(ctx, GrantAccessCommand(ALICE, D1, READ))
    before = len(facts(rig))
    request = AuthorizationRequest(READ, frozenset({D1}))
    assert rig.authorize.execute(ctx, request).allowed
    assert rig.resolve.execute(ctx, request).allowed
    denied = AuthorizationRequest(READ, frozenset({ResourceRef("document", "nope")}))
    assert not rig.authorize.execute(ctx, denied).allowed
    assert len(facts(rig)) == before  # ADR-019 D3: decision auditing is deferred


def test_the_fact_carries_no_signing_material_or_tokens(rig: Rig) -> None:
    ctx = ctx_for(TENANT_A, ALICE)
    rig.grant.execute(ctx, GrantAccessCommand(ALICE, D1, READ))
    decision = rig.authorize.execute(ctx, AuthorizationRequest(READ, frozenset({D1})))
    assert decision.proof is not None
    rendered = repr(facts(rig))
    assert decision.proof.token not in rendered
