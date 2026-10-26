from __future__ import annotations

from lda.modules.identity.application.attribution import attribution_of
from lda.modules.identity.application.dto.principal import (
    PrincipalView,
    RegisterPrincipalCommand,
    view_of,
)
from lda.modules.identity.application.ports import IdentityUnitOfWorkFactory
from lda.modules.identity.domain.display_name import DisplayName
from lda.modules.identity.domain.principal import Principal
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import DomainError, ValidationError
from lda.shared.kernel.ids import PrincipalId
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock, IdGenerator


@requires("identity.register_principal")
@audited("identity.principal_registered")
class RegisterPrincipal:
    """Create a new ACTIVE principal in the caller's tenant."""

    def __init__(
        self, units_of_work: IdentityUnitOfWorkFactory, clock: Clock, ids: IdGenerator
    ) -> None:
        self._units_of_work = units_of_work
        self._clock = clock
        self._ids = ids

    def execute(self, ctx: ExecutionContext, command: RegisterPrincipalCommand) -> PrincipalView:
        try:
            display_name = DisplayName(command.display_name)
        except DomainError as exc:
            raise ValidationError(exc.code) from None
        principal = Principal.register(
            id=PrincipalId(self._ids.new_uuid()),
            tenant_id=ctx.tenant_id,
            kind=command.kind,
            display_name=display_name,
            now=self._clock.now(),
        )
        with self._units_of_work.begin(ctx) as uow:
            uow.principals.add(principal)
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="identity.principal_registered",
                    resource_type="principal",
                    resource_id=str(principal.id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=principal.created_at,
                    attributes={**who.attributes, "principal_kind": principal.kind.value},
                )
            )
            uow.commit()
        return view_of(principal)
