from __future__ import annotations

from lda.modules.access_control.application.attribution import attribution_of
from lda.modules.access_control.application.dto.access import PolicyChangeView, RevokeAccessCommand
from lda.modules.access_control.application.ports import AccessControlUnitOfWorkFactory
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock


@requires("access_control.revoke_access")
@audited("access_control.access_revoked")
class RevokeAccess:
    """Remove one grant of the caller's tenant. Idempotent; works for stale or unregistered grants.

    The policy epoch advances in the same transaction, and only if a grant was actually removed.
    """

    def __init__(self, units_of_work: AccessControlUnitOfWorkFactory, clock: Clock) -> None:
        self._units_of_work = units_of_work
        self._clock = clock

    def execute(self, ctx: ExecutionContext, command: RevokeAccessCommand) -> PolicyChangeView:
        with self._units_of_work.begin(ctx) as uow:
            changed = uow.grants.remove(command.principal_id, command.resource, command.action)
            epoch = uow.policy_epochs.advance() if changed else uow.policy_epochs.current()
            if changed:  # revoking an absent grant changes nothing and is not audited (ADR-019 D5)
                who = attribution_of(ctx)
                uow.audit.record(
                    AuditFact(
                        tenant_id=ctx.tenant_id,
                        actor=who.actor,
                        action="access_control.access_revoked",
                        resource_type=command.resource.kind,
                        resource_id=command.resource.id,
                        outcome=AuditOutcome.SUCCESS,
                        request_id=who.request_id,
                        occurred_at=self._clock.now(),
                        attributes={
                            **who.attributes,
                            "principal_id": str(command.principal_id),
                            "revoked_action": str(command.action),
                            "policy_epoch": str(epoch.value),
                        },
                    )
                )
            uow.commit()
        return PolicyChangeView(changed=changed, policy_epoch=epoch.value)
