"""Reads the facts a decision rests on: live principal, registry, epoch, then grants (ADR-014)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lda.modules.access_control.application.dto.access import AuthorizationRequest
from lda.modules.access_control.application.ports import AccessControlUnitOfWorkFactory
from lda.modules.access_control.domain.decision import AuthorizationDecision, DenyReason
from lda.modules.access_control.domain.epoch import PolicyEpoch
from lda.modules.access_control.domain.proof import SigningKeyring, issue_proof
from lda.modules.access_control.domain.registry import ResourceRegistry
from lda.modules.access_control.domain.resource import ActionId, ResourceRef
from lda.modules.access_control.domain.scope import AccessScope
from lda.modules.identity.api import GetPrincipal, PrincipalReference
from lda.shared.kernel.context import ExecutionContext, TenantContext
from lda.shared.kernel.errors import AuthorizationError, NotFoundError
from lda.shared.kernel.ids import PrincipalId, TenantId


@dataclass(frozen=True, slots=True)
class PolicyFacts:
    tenant_id: TenantId
    principal_id: PrincipalId
    policy_epoch: PolicyEpoch
    granted: frozenset[ResourceRef]  # always a subset of what was requested

    def allow(
        self,
        action: ActionId,
        resources: frozenset[ResourceRef],
        now: datetime,
        keyring: SigningKeyring,
    ) -> AuthorizationDecision:
        """Freeze `resources` (the exact authorized set) into a scope and sign it."""
        scope = AccessScope(
            tenant_id=self.tenant_id,
            principal_id=self.principal_id,
            policy_epoch=self.policy_epoch,
            resources=resources,
        )
        return AuthorizationDecision.allow(action, scope, issue_proof(scope, action, now, keyring))


class PolicyReader:
    def __init__(
        self,
        units_of_work: AccessControlUnitOfWorkFactory,
        get_principal: GetPrincipal,
        registry: ResourceRegistry,
    ) -> None:
        self._units_of_work = units_of_work
        self._get_principal = get_principal
        self._registry = registry

    def read(
        self, ctx: ExecutionContext, request: AuthorizationRequest
    ) -> PolicyFacts | DenyReason:
        if not isinstance(ctx, TenantContext):
            raise AuthorizationError("authorization_requires_principal")
        # Identity is asked live on every decision; status is never cached (ADR-014 §9).
        try:
            principal = self._get_principal.execute(ctx, PrincipalReference(ctx.principal_id))
        except NotFoundError:
            return DenyReason.PRINCIPAL_NOT_ACTIVE
        if not principal.is_active:
            return DenyReason.PRINCIPAL_NOT_ACTIVE
        if not all(self._registry.allows(r.kind, request.action) for r in request.resources):
            return DenyReason.UNKNOWN_ACTION
        with self._units_of_work.begin(ctx) as uow:
            epoch = uow.policy_epochs.current()  # BEFORE the grants (ADR-014 §8)
            granted = uow.grants.granted(ctx.principal_id, request.action, request.resources)
        return PolicyFacts(
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            policy_epoch=epoch,
            granted=frozenset(granted) & request.resources,
        )
