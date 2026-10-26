"""In-memory Access Control adapters with the same observable behaviour as the SQL ones.

Writes are staged and reach the shared store only on commit, so rollback, tenant isolation and
epoch behaviour can be tested without a database. A contract suite runs against both adapters.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from lda.modules.access_control.domain.epoch import PolicyEpoch
from lda.modules.access_control.domain.grant import Grant
from lda.modules.access_control.domain.resource import ActionId, ResourceRef
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import JobId, PrincipalId, TenantId
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobSpec

GrantKey = tuple[PrincipalId, ResourceRef, ActionId]


class _Recorder:
    def __init__(self) -> None:
        self.facts: list[AuditFact] = []
        self.events: list[tuple[DomainEvent, Delivery]] = []
        self.jobs: list[JobSpec] = []

    def record(self, fact: AuditFact) -> None:
        self.facts.append(fact)

    def publish(self, event: DomainEvent, delivery: Delivery) -> None:
        self.events.append((event, delivery))

    def enqueue(self, spec: JobSpec) -> JobId | None:
        self.jobs.append(spec)
        return None


class InMemoryAccessControlStore:
    """The committed state, shared by every unit of work of one test."""

    def __init__(self) -> None:
        self.grants: dict[TenantId, set[GrantKey]] = {}
        self.epochs: dict[TenantId, int] = {}

    def grants_of(self, tenant_id: TenantId) -> set[GrantKey]:
        return set(self.grants.get(tenant_id, set()))

    def epoch_of(self, tenant_id: TenantId) -> int:
        return self.epochs.get(tenant_id, 0)


class InMemoryGrantRepository:
    def __init__(
        self,
        tenant_id: TenantId,
        store: InMemoryAccessControlStore,
        added: set[GrantKey],
        removed: set[GrantKey],
    ) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self._added = added
        self._removed = removed

    def _holds(self, key: GrantKey) -> bool:
        if key in self._added:
            return True
        return key in self._store.grants_of(self._tenant_id) and key not in self._removed

    def add(self, grant: Grant) -> bool:
        if grant.tenant_id != self._tenant_id:
            raise InfrastructureError("grant_tenant_mismatch")
        key = (grant.principal_id, grant.resource, grant.action)
        if self._holds(key):
            return False
        self._removed.discard(key)
        self._added.add(key)
        return True

    def remove(self, principal_id: PrincipalId, resource: ResourceRef, action: ActionId) -> bool:
        key = (principal_id, resource, action)
        if not self._holds(key):
            return False
        self._added.discard(key)
        self._removed.add(key)
        return True

    def remove_all_for_resource(self, resource: ResourceRef) -> int:
        held = {k for k in self._store.grants_of(self._tenant_id) | self._added if k[1] == resource}
        gone = {k for k in held if self._holds(k)}
        self._added -= gone
        self._removed |= gone
        return len(gone)

    def granted(
        self, principal_id: PrincipalId, action: ActionId, candidates: frozenset[ResourceRef]
    ) -> frozenset[ResourceRef]:
        return frozenset(c for c in candidates if self._holds((principal_id, c, action)))


class InMemoryPolicyEpochRepository:
    def __init__(self, tenant_id: TenantId, store: InMemoryAccessControlStore) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self.advances = 0

    def current(self) -> PolicyEpoch:
        return PolicyEpoch(self._store.epoch_of(self._tenant_id) + self.advances)

    def advance(self) -> PolicyEpoch:
        self.advances += 1
        return self.current()


class InMemoryAccessControlUnitOfWork:
    def __init__(self, tenant_id: TenantId, store: InMemoryAccessControlStore) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self._added: set[GrantKey] = set()
        self._removed: set[GrantKey] = set()
        self._grants = InMemoryGrantRepository(tenant_id, store, self._added, self._removed)
        self._policy_epochs = InMemoryPolicyEpochRepository(tenant_id, store)
        self._shared = _Recorder()
        self.committed = False
        self.rolled_back = False

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    @property
    def events(self) -> _Recorder:
        return self._shared

    @property
    def jobs(self) -> _Recorder:
        return self._shared

    @property
    def audit(self) -> _Recorder:
        return self._shared

    @property
    def grants(self) -> InMemoryGrantRepository:
        return self._grants

    @property
    def policy_epochs(self) -> InMemoryPolicyEpochRepository:
        return self._policy_epochs

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.committed:
            self.rolled_back = True

    def commit(self) -> None:
        # Grants and the epoch move together, like one SQL transaction.
        held = self._store.grants.setdefault(self._tenant_id, set())
        held |= self._added
        held -= self._removed
        advances = self._policy_epochs.advances
        if advances:
            self._store.epochs[self._tenant_id] = self._store.epoch_of(self._tenant_id) + advances
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class InMemoryAccessControlUnitOfWorkFactory:
    def __init__(self, store: InMemoryAccessControlStore | None = None) -> None:
        self.store = store or InMemoryAccessControlStore()
        self.begun: list[InMemoryAccessControlUnitOfWork] = []

    def begin(self, ctx: ExecutionContext) -> InMemoryAccessControlUnitOfWork:
        uow = InMemoryAccessControlUnitOfWork(ctx.tenant_id, self.store)
        self.begun.append(uow)
        return uow
