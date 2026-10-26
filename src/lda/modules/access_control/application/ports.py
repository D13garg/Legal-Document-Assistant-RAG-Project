"""Ports Access Control's use cases need. Adapters: infrastructure (SQL) and testing (in-memory)."""

from __future__ import annotations

from typing import Protocol

from lda.modules.access_control.domain.epoch import PolicyEpoch
from lda.modules.access_control.domain.grant import Grant
from lda.modules.access_control.domain.resource import ActionId, ResourceRef
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.ids import PrincipalId
from lda.shared.ports.unit_of_work import UnitOfWork


class GrantRepository(Protocol):
    """Bound to one tenant for its whole life: it cannot see or change another tenant's grants."""

    def add(self, grant: Grant) -> bool:
        """Insert the grant. False (and no change) if it already exists."""
        ...

    def remove(self, principal_id: PrincipalId, resource: ResourceRef, action: ActionId) -> bool:
        """Delete the grant. False (and no change) if it does not exist."""
        ...

    def remove_all_for_resource(self, resource: ResourceRef) -> int:
        """Delete every grant on the resource (any principal, any action); return how many."""
        ...

    def granted(
        self, principal_id: PrincipalId, action: ActionId, candidates: frozenset[ResourceRef]
    ) -> frozenset[ResourceRef]:
        """The subset of `candidates` this principal holds for `action` (never more)."""
        ...


class PolicyEpochRepository(Protocol):
    """The bound tenant's epoch. Absent means 0."""

    def current(self) -> PolicyEpoch: ...

    def advance(self) -> PolicyEpoch:
        """Add exactly 1 inside the caller's transaction and return the new value."""
        ...


class AccessControlUnitOfWork(UnitOfWork, Protocol):
    @property
    def grants(self) -> GrantRepository: ...

    @property
    def policy_epochs(self) -> PolicyEpochRepository: ...


class AccessControlUnitOfWorkFactory(Protocol):
    def begin(self, ctx: ExecutionContext) -> AccessControlUnitOfWork: ...
