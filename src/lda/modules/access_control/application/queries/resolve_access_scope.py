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
class ResolveAccessScope:
    """Filter: the scope is exactly the granted subset of the candidates; none granted is DENY."""

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
        if not facts.granted:
            return AuthorizationDecision.deny(request.action, DenyReason.NOT_GRANTED)
        return facts.allow(request.action, facts.granted, self._clock.now(), self._keyring)
