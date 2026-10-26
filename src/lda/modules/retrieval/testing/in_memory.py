"""In-memory retrieval adapters with the same observable behaviour as the SQL ones.

A unit of work works on copies and publishes them only on `commit`, so rollback, tenant isolation
and the fence can be tested without a database. A contract suite runs against both adapters.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from types import TracebackType
from typing import Self

from lda.modules.retrieval.domain.embedding import Vector
from lda.modules.retrieval.domain.ids import DocumentId, VersionId
from lda.modules.retrieval.domain.record import IndexedChunk, IndexedVersion, PurgeReceipt
from lda.modules.retrieval.domain.selection import Candidate
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError, ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import JobId, TenantId
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobSpec


@dataclass(frozen=True, slots=True)
class StoredRow:
    record: IndexedChunk
    document_id: DocumentId
    version_id: VersionId
    index_id: str


class InMemoryRetrievalStore:
    """The committed state, shared by every unit of work of one test."""

    def __init__(self) -> None:
        self.versions: dict[tuple[TenantId, VersionId, str], IndexedVersion] = {}
        self.rows: dict[tuple[TenantId, str, object], StoredRow] = {}
        self.receipts: dict[tuple[TenantId, DocumentId], PurgeReceipt] = {}
        self.fence_rejects: Callable[[JobId, int], bool] | None = None
        self.fail_commit_on: set[int] = set()  # 0-based index of units of work whose commit fails


class _NothingToRecord:
    """retrieval publishes no event, enqueues no job and writes no audit fact."""

    def record(self, fact: AuditFact) -> None:
        raise InfrastructureError("not_used_by_retrieval")

    def publish(self, event: DomainEvent, delivery: Delivery) -> None:
        raise InfrastructureError("not_used_by_retrieval")

    def enqueue(self, spec: JobSpec) -> JobId | None:
        raise InfrastructureError("not_used_by_retrieval")


class _Fence:
    def __init__(self, store: InMemoryRetrievalStore) -> None:
        self._store = store

    def verify(self, job_id: JobId, fencing_token: int) -> None:
        rejects = self._store.fence_rejects
        if rejects is not None and rejects(job_id, fencing_token):
            raise ConflictError("job_lease_lost")


def _similarity(a: Vector, b: Vector) -> float:
    return math.fsum(x * y for x, y in zip(a, b, strict=True))  # both unit length


class InMemoryVectorIndexRepository:
    def __init__(
        self,
        tenant_id: TenantId,
        versions: dict[tuple[TenantId, VersionId, str], IndexedVersion],
        rows: dict[tuple[TenantId, str, object], StoredRow],
        locked: list[DocumentId],
    ) -> None:
        self._tenant_id = tenant_id
        self._versions = versions
        self._rows = rows
        self._locked = locked

    def add_version(self, record: IndexedVersion, chunks: Sequence[IndexedChunk]) -> bool:
        if record.tenant_id != self._tenant_id:
            raise InfrastructureError("retrieval_tenant_mismatch")
        if len(chunks) != record.chunk_count:
            raise ValidationError("index_record_counts_invalid")
        key = (self._tenant_id, record.version_id, record.index_id)
        if key in self._versions:
            return False
        self._versions[key] = record
        for chunk in chunks:
            self._rows[(self._tenant_id, record.index_id, chunk.chunk_id)] = StoredRow(
                chunk, record.document_id, record.version_id, record.index_id
            )
        return True

    def get_version(self, version_id: VersionId, index_id: str) -> IndexedVersion | None:
        return self._versions.get((self._tenant_id, version_id, index_id))

    def versions_of(
        self, documents: Sequence[DocumentId]
    ) -> dict[DocumentId, tuple[VersionId, ...]]:
        wanted = set(documents)
        found: dict[DocumentId, set[VersionId]] = {}
        for (tenant, _, _), record in self._versions.items():
            if tenant == self._tenant_id and record.document_id in wanted:
                found.setdefault(record.document_id, set()).add(record.version_id)
        return {d: tuple(sorted(v, key=str)) for d, v in found.items()}

    def nearest(
        self,
        index_id: str,
        documents: Sequence[DocumentId],
        query: Vector,
        limit: int,
        offset: int,
    ) -> tuple[Candidate, ...]:
        if limit < 1 or offset < 0:
            raise ValidationError("search_page_invalid")
        wanted = set(documents)
        ranked = sorted(
            (
                (_similarity(row.record.vector, query), row)
                for (tenant, ix, _), row in self._rows.items()
                if tenant == self._tenant_id and ix == index_id and row.document_id in wanted
            ),
            key=lambda pair: (-pair[0], str(pair[1].record.chunk_id)),
        )
        return tuple(
            Candidate(
                r.document_id,
                r.version_id,
                r.record.chunk_id,
                r.record.ordinal,
                r.record.text_hash,
                s,
            )
            for s, r in ranked[offset : offset + limit]
        )

    def list_for(self, document_id: DocumentId) -> tuple[VersionId, ...]:
        found = {
            r.version_id
            for (tenant, _, _), r in self._versions.items()
            if tenant == self._tenant_id and r.document_id == document_id
        }
        return tuple(sorted(found, key=str))

    def remove_all_for(self, document_id: DocumentId) -> int:
        doomed_rows = [
            k
            for k, r in self._rows.items()
            if k[0] == self._tenant_id and r.document_id == document_id
        ]
        for row_key in doomed_rows:
            del self._rows[row_key]
        doomed = [
            k
            for k, r in self._versions.items()
            if k[0] == self._tenant_id and r.document_id == document_id
        ]
        for version_key in doomed:
            del self._versions[version_key]
        return len(doomed)

    def lock_document(self, document_id: DocumentId) -> None:
        """Recorded so tests can see that every writer of derived data takes the lifecycle lock."""
        self._locked.append(document_id)


class InMemoryReceiptRepository:
    def __init__(
        self, tenant_id: TenantId, working: dict[tuple[TenantId, DocumentId], PurgeReceipt]
    ) -> None:
        self._tenant_id = tenant_id
        self._working = working

    def add(self, receipt: PurgeReceipt) -> bool:
        if receipt.tenant_id != self._tenant_id:
            raise InfrastructureError("retrieval_tenant_mismatch")
        key = (self._tenant_id, receipt.document_id)
        if key in self._working:
            return False
        self._working[key] = receipt
        return True

    def get(self, document_id: DocumentId) -> PurgeReceipt | None:
        return self._working.get((self._tenant_id, document_id))


class InMemoryRetrievalUnitOfWork:
    def __init__(self, tenant_id: TenantId, store: InMemoryRetrievalStore, index: int) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self._index_number = index
        self.version_rows = {k: replace(v) for k, v in store.versions.items()}
        self.vector_rows = dict(store.rows)
        self.store = store
        self._receipts_working = dict(store.receipts)
        self.locked: list[DocumentId] = []
        self._index = InMemoryVectorIndexRepository(
            tenant_id, self.version_rows, self.vector_rows, self.locked
        )
        self._receipts = InMemoryReceiptRepository(tenant_id, self._receipts_working)
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
    def index(self) -> InMemoryVectorIndexRepository:
        return self._index

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
        if self._index_number in self._store.fail_commit_on:
            raise InfrastructureError("database_unavailable")
        self._store.versions = self.version_rows
        self._store.rows = self.vector_rows
        self._store.receipts = self._receipts_working
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class InMemoryRetrievalUnitOfWorkFactory:
    def __init__(self, store: InMemoryRetrievalStore | None = None) -> None:
        self.store = store or InMemoryRetrievalStore()
        self.begun: list[InMemoryRetrievalUnitOfWork] = []

    def begin(self, ctx: ExecutionContext) -> InMemoryRetrievalUnitOfWork:
        uow = InMemoryRetrievalUnitOfWork(ctx.tenant_id, self.store, len(self.begun))
        self.begun.append(uow)
        return uow
