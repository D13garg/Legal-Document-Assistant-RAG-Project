"""The outcome of an authorization decision (ADR-014 §10)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lda.modules.access_control.domain.errors import InvalidDecision
from lda.modules.access_control.domain.proof import AuthorizationProof
from lda.modules.access_control.domain.resource import ActionId
from lda.modules.access_control.domain.scope import AccessScope


class Effect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class DenyReason(StrEnum):
    PRINCIPAL_NOT_ACTIVE = "principal_not_active"
    NOT_GRANTED = "not_granted"
    UNKNOWN_ACTION = "unknown_action"


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """ALLOW carries the exact scope and its proof; DENY carries a reason and nothing else."""

    effect: Effect
    action: ActionId
    scope: AccessScope | None
    proof: AuthorizationProof | None
    reason: DenyReason | None

    def __post_init__(self) -> None:
        allowed = self.effect is Effect.ALLOW
        if allowed:
            consistent = (
                self.scope is not None
                and bool(self.scope.resources)
                and self.proof is not None
                and self.reason is None
            )
        else:
            consistent = self.scope is None and self.proof is None and self.reason is not None
        if not consistent:
            raise InvalidDecision

    @classmethod
    def allow(
        cls, action: ActionId, scope: AccessScope, proof: AuthorizationProof
    ) -> AuthorizationDecision:
        return cls(Effect.ALLOW, action, scope, proof, None)

    @classmethod
    def deny(cls, action: ActionId, reason: DenyReason) -> AuthorizationDecision:
        return cls(Effect.DENY, action, None, None, reason)

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW
