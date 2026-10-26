"""Ports of chunking. Adapters: infrastructure (SQL) and testing (in-memory)."""

from __future__ import annotations

from typing import Protocol

from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.record import ChunkSetRecord, PurgeReceipt
from lda.shared.kernel.context import ExecutionContext
from lda.shared.ports.jobs import JobFence
from lda.shared.ports.unit_of_work import UnitOfWork


class ChunkSetRepository(Protocol):
    """Bound to one tenant for its whole life."""

    def add(self, record: ChunkSetRecord) -> bool:
        """Insert if absent. False when the version already has a record (never replaced)."""
        ...

    def get(self, version_id: VersionId) -> ChunkSetRecord | None: ...

    def list_for(self, document_id: DocumentId) -> tuple[ChunkSetRecord, ...]: ...

    def remove_all_for(self, document_id: DocumentId) -> int: ...

    def lock_document(self, document_id: DocumentId) -> None:
        """Serializes, until the transaction ends, every writer of this document's derived data
        with its purge: a record is never added after the purge's receipt, and the purge sees
        every record added before it."""
        ...


class ReceiptRepository(Protocol):
    def add(self, receipt: PurgeReceipt) -> bool:
        """Insert if absent. False when a receipt exists (receipts are never replaced)."""
        ...

    def get(self, document_id: DocumentId) -> PurgeReceipt | None: ...


class ChunkingUnitOfWork(UnitOfWork, Protocol):
    @property
    def chunk_sets(self) -> ChunkSetRepository: ...

    @property
    def receipts(self) -> ReceiptRepository: ...

    @property
    def fence(self) -> JobFence: ...


class ChunkingUnitOfWorkFactory(Protocol):
    def begin(self, ctx: ExecutionContext) -> ChunkingUnitOfWork: ...
