from __future__ import annotations

from lda.modules.access_control.application.dto.access import AuthorizationRequest
from lda.modules.access_control.application.policy_reader import PolicyReader
from lda.modules.access_control.domain.decision import AuthorizationDecision, DenyReason
from lda.modules.access_control.domain.proof import SigningKeyring
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.ports.clock import Clock


@public(reason="The decision point itself: callers declare needs; it answers only for the caller")
@not_audited(reason="Decision auditing is designed in the audit module (ADR-019); hot read path")
class AuthorizeAction:
    """Strict: ALLOW only if the caller holds the action on EVERY requested resource."""

    def __init__(self, policy: PolicyReader, clock: Clock, keyring: SigningKeyring) -> None:
        self._policy = policy
        self._clock = clock
        self._keyring = keyring

    def execute(
        self, ctx: ExecutionContext, request: AuthorizationRequest
    ) -> AuthorizationDecision:
        facts = self._policy.read(ctx, request)
        if isinstance(facts, DenyReason):
            return AuthorizationDecision.deny(request.action, facts)
        if facts.granted != request.resources:
            return AuthorizationDecision.deny(request.action, DenyReason.NOT_GRANTED)
        return facts.allow(request.action, request.resources, self._clock.now(), self._keyring)
