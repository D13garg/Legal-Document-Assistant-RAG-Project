"""In-memory Identity adapters with the same observable behaviour as the SQL ones.

Writes are staged and reach the shared store only on commit, so rollback and tenant isolation can
be tested without a database. A contract suite runs against both implementations.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from lda.modules.identity.domain.principal import Principal
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import JobId, PrincipalId, TenantId
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobSpec


class _Recorder:
    """Identity's use cases never publish, enqueue or audit; the shared services just record."""

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


class InMemoryIdentityStore:
    """The committed state, shared by every unit of work of one test."""

    def __init__(self) -> None:
        self.rows: dict[tuple[TenantId, PrincipalId], Principal] = {}

    def all_for(self, tenant_id: TenantId) -> list[Principal]:
        return [p for (t, _), p in self.rows.items() if t == tenant_id]


Key = tuple[TenantId, PrincipalId]


class InMemoryPrincipalRepository:
    """Staged writes over a committed store. Like the SQL adapter, a principal changed by someone
    else since this unit of work began cannot be saved over (optimistic version check)."""

    def __init__(
        self,
        tenant_id: TenantId,
        staged: dict[Key, Principal],
        committed: dict[Key, Principal],
        touched: set[Key],
    ) -> None:
        self._tenant_id = tenant_id
        self._staged = staged
        self._committed = committed
        self._touched = touched

    def add(self, principal: Principal) -> None:
        if principal.tenant_id != self._tenant_id:
            raise InfrastructureError("principal_tenant_mismatch")
        known = [*self._staged, *self._committed]
        if any(known_id == principal.id for _, known_id in known):  # ids are globally unique
            raise ConflictError("principal_exists")
        key = (self._tenant_id, principal.id)
        self._staged[key] = principal
        self._touched.add(key)

    def get(self, principal_id: PrincipalId) -> Principal | None:
        return self._staged.get((self._tenant_id, principal_id))

    def save(self, principal: Principal) -> None:
        key = (self._tenant_id, principal.id)
        current = self._staged.get(key)
        if current is None or current.version != principal.version - 1:
            raise ConflictError("principal_state_changed")
        if key not in self._touched:
            latest = self._committed.get(key)
            if latest is None or latest.version != current.version:
                raise ConflictError("principal_state_changed")
        self._staged[key] = principal
        self._touched.add(key)


class InMemoryIdentityUnitOfWork:
    def __init__(self, tenant_id: TenantId, store: InMemoryIdentityStore) -> None:
        self._store = store
        self._staged = dict(store.rows)
        self._touched: set[Key] = set()
        self._principals = InMemoryPrincipalRepository(
            tenant_id, self._staged, store.rows, self._touched
        )
        self._tenant_id = tenant_id
        self._shared = _Recorder()
        self.committed = False
        self.rolled_back = False

    def _publish(self) -> None:
        for key in self._touched:
            base, latest = self._staged[key].version, self._store.rows.get(key)
            if latest is not None and latest.version >= base:
                raise ConflictError("principal_state_changed")  # someone committed first
        for key in self._touched:
            self._store.rows[key] = self._staged[key]

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
    def principals(self) -> InMemoryPrincipalRepository:
        return self._principals

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
        self._publish()
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class InMemoryIdentityUnitOfWorkFactory:
    def __init__(self, store: InMemoryIdentityStore | None = None) -> None:
        self.store = store or InMemoryIdentityStore()
        self.begun: list[InMemoryIdentityUnitOfWork] = []

    def begin(self, ctx: ExecutionContext) -> InMemoryIdentityUnitOfWork:
        uow = InMemoryIdentityUnitOfWork(ctx.tenant_id, self.store)
        self.begun.append(uow)
        return uow
