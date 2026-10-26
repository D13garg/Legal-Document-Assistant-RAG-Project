from __future__ import annotations

from lda.modules.identity.application.attribution import attribution_of
from lda.modules.identity.application.dto.principal import (
    PrincipalReference,
    PrincipalView,
    view_of,
)
from lda.modules.identity.application.ports import IdentityUnitOfWorkFactory
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import NotFoundError
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock


@requires("identity.disable_principal")
@audited("identity.principal_disabled")
class DisablePrincipal:
    """Disable a principal of the caller's tenant. Idempotent when already disabled."""

    def __init__(self, units_of_work: IdentityUnitOfWorkFactory, clock: Clock) -> None:
        self._units_of_work = units_of_work
        self._clock = clock

    def execute(self, ctx: ExecutionContext, command: PrincipalReference) -> PrincipalView:
        with self._units_of_work.begin(ctx) as uow:
            principal = uow.principals.get(command.principal_id)
            if principal is None:
                raise NotFoundError("principal_not_found")
            if not principal.is_active:
                return view_of(principal)
            disabled = principal.disable(self._clock.now())
            uow.principals.save(disabled)
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="identity.principal_disabled",
                    resource_type="principal",
                    resource_id=str(command.principal_id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=disabled.status_changed_at,
                    attributes=who.attributes,
                )
            )
            uow.commit()
        return view_of(disabled)
