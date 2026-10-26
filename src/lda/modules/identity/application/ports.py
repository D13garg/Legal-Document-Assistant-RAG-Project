"""Ports Identity's use cases need. Adapters: infrastructure (SQL) and testing (in-memory)."""

from __future__ import annotations

from typing import Protocol

from lda.modules.identity.domain.principal import Principal
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.ids import PrincipalId
from lda.shared.ports.unit_of_work import UnitOfWork


class PrincipalRepository(Protocol):
    """Bound to one tenant for its whole life: it cannot see or write another tenant's rows."""

    def add(self, principal: Principal) -> None:
        """Insert a new principal. Raises ConflictError('principal_exists') on a duplicate id."""
        ...

    def get(self, principal_id: PrincipalId) -> Principal | None:
        """The principal of the bound tenant, or None (another tenant's id is simply absent)."""
        ...

    def save(self, principal: Principal) -> None:
        """Persist a lifecycle move made from `principal.version - 1`.

        Raises ConflictError('principal_state_changed') if the stored version is not the one the
        move started from (a concurrent change won).
        """
        ...


class IdentityUnitOfWork(UnitOfWork, Protocol):
    @property
    def principals(self) -> PrincipalRepository: ...


class IdentityUnitOfWorkFactory(Protocol):
    def begin(self, ctx: ExecutionContext) -> IdentityUnitOfWork: ...
