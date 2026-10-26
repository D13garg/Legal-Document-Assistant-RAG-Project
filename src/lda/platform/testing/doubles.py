from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

from lda.shared.kernel.errors import ConflictError, NotFoundError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import JobId, TenantId
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobSpec
from lda.shared.ports.object_store import StoredObject, TenantScopedKey


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class SequentialIdGenerator:
    def __init__(self) -> None:
        self._n = 0

    def new_uuid(self) -> UUID:
        self._n += 1
        return UUID(int=self._n)


class InMemoryObjectStore:
    def __init__(self, presign_max_seconds: int = 900) -> None:
        self._objects: dict[str, bytes] = {}
        self._presign_max = presign_max_seconds

    def put(
        self, key: TenantScopedKey, body: bytes, *, content_type: str, overwrite: bool = False
    ) -> StoredObject:
        if key.render() in self._objects and not overwrite:
            raise ConflictError("object_exists")
        self._objects[key.render()] = body
        return StoredObject(key, len(body))

    def get(self, key: TenantScopedKey) -> bytes:
        try:
            return self._objects[key.render()]
        except KeyError:
            raise NotFoundError("object_not_found") from None

    def exists(self, key: TenantScopedKey) -> bool:
        return key.render() in self._objects

    def delete(self, key: TenantScopedKey) -> None:
        self._objects.pop(key.render(), None)

    def presign_get(self, key: TenantScopedKey, *, expires_seconds: int) -> str:
        if not 1 <= expires_seconds <= self._presign_max:
            raise ValueError("expires_seconds out of allowed range")
        return f"memory://{key.render()}?expires={expires_seconds}"


class InMemoryJobQueue:
    def __init__(self, tenant_id: TenantId, ids: SequentialIdGenerator | None = None) -> None:
        self._tenant_id = tenant_id
        self._ids = ids or SequentialIdGenerator()
        self.jobs: list[tuple[JobId, JobSpec]] = []

    def enqueue(self, spec: JobSpec) -> JobId | None:
        if spec.unique_key and any(s.unique_key == spec.unique_key for _, s in self.jobs):
            return None
        job_id = JobId(self._ids.new_uuid())
        self.jobs.append((job_id, spec))
        return job_id


class InMemoryAuditRecorder:
    def __init__(self) -> None:
        self.facts: list[AuditFact] = []

    def record(self, fact: AuditFact) -> None:
        self.facts.append(fact)


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[DomainEvent, Delivery]] = []

    def publish(self, event: DomainEvent, delivery: Delivery) -> None:
        self.published.append((event, delivery))


class InMemoryUnitOfWork:
    """Collects events/jobs/audit; `committed`/`rolled_back` expose the outcome to tests."""

    def __init__(
        self,
        tenant_id: TenantId,
        on_commit: Callable[[], None] | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self.events = RecordingEventPublisher()
        self.jobs = InMemoryJobQueue(tenant_id)
        self.audit = InMemoryAuditRecorder()
        self.committed = False
        self.rolled_back = False
        self._on_commit = on_commit

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

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
        self.committed = True
        if self._on_commit:
            self._on_commit()

    def rollback(self) -> None:
        self.rolled_back = True
