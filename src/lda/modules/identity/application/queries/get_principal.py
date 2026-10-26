from __future__ import annotations

from lda.modules.identity.application.dto.principal import (
    PrincipalReference,
    PrincipalView,
    view_of,
)
from lda.modules.identity.application.ports import IdentityUnitOfWorkFactory
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import NotFoundError


@public(reason="Answers WHO a principal is; authorization itself must be able to ask it")
@not_audited(reason="Read-only, high-volume lookup of identity facts")
class GetPrincipal:
    """Read a principal of the caller's tenant; another tenant's id looks absent."""

    def __init__(self, units_of_work: IdentityUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def execute(self, ctx: ExecutionContext, query: PrincipalReference) -> PrincipalView:
        with self._units_of_work.begin(ctx) as uow:
            principal = uow.principals.get(query.principal_id)
        if principal is None:
            raise NotFoundError("principal_not_found")
        return view_of(principal)
