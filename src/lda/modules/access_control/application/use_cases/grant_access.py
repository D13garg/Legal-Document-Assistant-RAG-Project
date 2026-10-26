from __future__ import annotations

from lda.modules.access_control.application.attribution import attribution_of
from lda.modules.access_control.application.dto.access import GrantAccessCommand, PolicyChangeView
from lda.modules.access_control.application.ports import AccessControlUnitOfWorkFactory
from lda.modules.access_control.domain.grant import Grant
from lda.modules.access_control.domain.registry import ResourceRegistry
from lda.modules.identity.api import GetPrincipal, PrincipalReference
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import ValidationError
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock


@requires("access_control.grant_access")
@audited("access_control.access_granted")
class GrantAccess:
    """Grant one principal of the caller's tenant one action on one resource. Idempotent.

    The policy epoch advances in the same transaction, and only if the grant set changed.
    """

    def __init__(
        self,
        units_of_work: AccessControlUnitOfWorkFactory,
        get_principal: GetPrincipal,
        registry: ResourceRegistry,
        clock: Clock,
    ) -> None:
        self._units_of_work = units_of_work
        self._get_principal = get_principal
        self._registry = registry
        self._clock = clock

    def execute(self, ctx: ExecutionContext, command: GrantAccessCommand) -> PolicyChangeView:
        if not self._registry.allows(command.resource.kind, command.action):
            raise ValidationError("action_not_allowed_for_resource")
        # Raises NotFoundError('principal_not_found') for an absent or other-tenant principal.
        self._get_principal.execute(ctx, PrincipalReference(command.principal_id))
        grant = Grant(
            tenant_id=ctx.tenant_id,
            principal_id=command.principal_id,
            resource=command.resource,
            action=command.action,
            granted_at=self._clock.now(),
        )
        with self._units_of_work.begin(ctx) as uow:
            changed = uow.grants.add(grant)
            epoch = uow.policy_epochs.advance() if changed else uow.policy_epochs.current()
            if changed:  # an idempotent repeat changes nothing and is not audited (ADR-019 D5)
                who = attribution_of(ctx)
                uow.audit.record(
                    AuditFact(
                        tenant_id=ctx.tenant_id,
                        actor=who.actor,
                        action="access_control.access_granted",
                        resource_type=command.resource.kind,
                        resource_id=command.resource.id,
                        outcome=AuditOutcome.SUCCESS,
                        request_id=who.request_id,
                        occurred_at=grant.granted_at,
                        attributes={
                            **who.attributes,
                            "principal_id": str(command.principal_id),
                            "granted_action": str(command.action),
                            "policy_epoch": str(epoch.value),
                        },
                    )
                )
            uow.commit()
        return PolicyChangeView(changed=changed, policy_epoch=epoch.value)
