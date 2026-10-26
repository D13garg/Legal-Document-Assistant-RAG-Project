"""In-memory chunking adapters with the same observable behaviour as the SQL ones.

A unit of work works on copies and publishes them only on `commit`, so rollback, tenant isolation
and the fence can be tested without a database. A contract suite runs against both adapters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from types import TracebackType
from typing import Self

from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.record import ChunkSetRecord, PurgeReceipt
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import JobId, TenantId
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobSpec

ChunkSetKey = tuple[TenantId, VersionId]
ReceiptKey = tuple[TenantId, DocumentId]


class InMemoryChunkingStore:
    """The committed state, shared by every unit of work of one test."""

    def __init__(self) -> None:
        self.chunk_sets: dict[ChunkSetKey, ChunkSetRecord] = {}
        self.receipts: dict[ReceiptKey, PurgeReceipt] = {}
        # Tests make the fence fail like a lost lease: return True to reject (job, token).
        self.fence_rejects: Callable[[JobId, int], bool] | None = None
        self.fail_commit_on: set[int] = set()  # 0-based index of units of work whose commit fails


class _NothingToRecord:
    """chunking publishes no event, enqueues no job and writes no audit fact."""

    def record(self, fact: AuditFact) -> None:
        raise InfrastructureError("not_used_by_chunking")

    def publish(self, event: DomainEvent, delivery: Delivery) -> None:
        raise InfrastructureError("not_used_by_chunking")

    def enqueue(self, spec: JobSpec) -> JobId | None:
        raise InfrastructureError("not_used_by_chunking")


class _Fence:
    def __init__(self, store: InMemoryChunkingStore) -> None:
        self._store = store

    def verify(self, job_id: JobId, fencing_token: int) -> None:
        rejects = self._store.fence_rejects
        if rejects is not None and rejects(job_id, fencing_token):
            raise ConflictError("job_lease_lost")


class InMemoryChunkSetRepository:
    def __init__(
        self,
        tenant_id: TenantId,
        working: dict[ChunkSetKey, ChunkSetRecord],
        locked: list[DocumentId],
    ) -> None:
        self._tenant_id = tenant_id
        self._working = working
        self._locked = locked

    def _check(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant_id:
            raise InfrastructureError("chunking_tenant_mismatch")

    def add(self, record: ChunkSetRecord) -> bool:
        self._check(record.tenant_id)
        key = (self._tenant_id, record.version_id)
        if key in self._working:
            return False
        self._working[key] = record
        return True

    def get(self, version_id: VersionId) -> ChunkSetRecord | None:
        return self._working.get((self._tenant_id, version_id))

    def list_for(self, document_id: DocumentId) -> tuple[ChunkSetRecord, ...]:
        mine = [
            r
            for (tenant, _), r in self._working.items()
            if tenant == self._tenant_id and r.document_id == document_id
        ]
        return tuple(sorted(mine, key=lambda r: str(r.version_id)))

    def lock_document(self, document_id: DocumentId) -> None:
        """A unit of work is single-threaded here; the call is recorded so tests can see that every
        writer of derived data takes the lifecycle lock."""
        self._locked.append(document_id)

    def remove_all_for(self, document_id: DocumentId) -> int:
        doomed = [
            key
            for key, r in self._working.items()
            if key[0] == self._tenant_id and r.document_id == document_id
        ]
        for key in doomed:
            del self._working[key]
        return len(doomed)


class InMemoryReceiptRepository:
    def __init__(self, tenant_id: TenantId, working: dict[ReceiptKey, PurgeReceipt]) -> None:
        self._tenant_id = tenant_id
        self._working = working

    def add(self, receipt: PurgeReceipt) -> bool:
        if receipt.tenant_id != self._tenant_id:
            raise InfrastructureError("chunking_tenant_mismatch")
        key = (self._tenant_id, receipt.document_id)
        if key in self._working:
            return False
        self._working[key] = receipt
        return True

    def get(self, document_id: DocumentId) -> PurgeReceipt | None:
        return self._working.get((self._tenant_id, document_id))


class InMemoryChunkingUnitOfWork:
    def __init__(self, tenant_id: TenantId, store: InMemoryChunkingStore, index: int) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self._index = index
        self._working_chunk_sets = {k: replace(v) for k, v in store.chunk_sets.items()}
        self._working_receipts = dict(store.receipts)
        self.locked: list[DocumentId] = []
        self._chunk_sets = InMemoryChunkSetRepository(
            tenant_id, self._working_chunk_sets, self.locked
        )
        self._receipts = InMemoryReceiptRepository(tenant_id, self._working_receipts)
        self._nothing = _NothingToRecord()
        self._fence = _Fence(store)
        self.committed = False
        self.rolled_back = False

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    @property
    def events(self) -> _NothingToRecord:
        return self._nothing

    @property
    def jobs(self) -> _NothingToRecord:
        return self._nothing

    @property
    def audit(self) -> _NothingToRecord:
        return self._nothing

    @property
    def fence(self) -> _Fence:
        return self._fence

    @property
    def chunk_sets(self) -> InMemoryChunkSetRepository:
        return self._chunk_sets

    @property
    def receipts(self) -> InMemoryReceiptRepository:
        return self._receipts

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
        if self._index in self._store.fail_commit_on:
            raise InfrastructureError("database_unavailable")
        self._store.chunk_sets = self._working_chunk_sets
        self._store.receipts = self._working_receipts
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class InMemoryChunkingUnitOfWorkFactory:
    def __init__(self, store: InMemoryChunkingStore | None = None) -> None:
        self.store = store or InMemoryChunkingStore()
        self.begun: list[InMemoryChunkingUnitOfWork] = []

    def begin(self, ctx: ExecutionContext) -> InMemoryChunkingUnitOfWork:
        uow = InMemoryChunkingUnitOfWork(ctx.tenant_id, self.store, len(self.begun))
        self.begun.append(uow)
        return uow
