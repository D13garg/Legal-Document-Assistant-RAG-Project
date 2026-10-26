"""Ports of retrieval. Adapters: infrastructure (SQL, other modules' APIs), testing (fakes)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from lda.modules.retrieval.application.source import SourceChunks
from lda.modules.retrieval.domain.embedding import EmbeddingIdentity, Vector
from lda.modules.retrieval.domain.ids import DocumentId, VersionId
from lda.modules.retrieval.domain.record import IndexedChunk, IndexedVersion, PurgeReceipt
from lda.modules.retrieval.domain.selection import Candidate, VersionFact
from lda.shared.kernel.context import ExecutionContext, SystemContext
from lda.shared.ports.jobs import JobFence
from lda.shared.ports.unit_of_work import UnitOfWork


class Embedder(Protocol):
    """Turns text into unit vectors of one fixed identity (ADR-007).

    Text is content. An implementation that sends it out of the process is a provider egress and is
    NOT permitted until an approved rule decides how embedding egress is authorized (ADR-026 does
    not yet; ADR-007 records the gap). Implementations are a deterministic test double and, by the
    ADR-007 amendment (ADR-045 D3), one OFFLINE development embedder: no egress either way.
    """

    @property
    def identity(self) -> EmbeddingIdentity: ...

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...


class ChunkSource(Protocol):
    """The chunk text and ids of one eligible version, read through the owning module."""

    def read(
        self, ctx: SystemContext, document_id: DocumentId, version_id: VersionId
    ) -> SourceChunks:
        """Raises `RetrievalRejected` with a permanent code when the version cannot be read;
        any other exception is transient."""
        ...


class VersionStates(Protocol):
    """Live lifecycle facts from Documents: the only source of status and version number."""

    def read(
        self, ctx: SystemContext, document_id: DocumentId, version_id: VersionId
    ) -> VersionFact | None:
        """None when Documents does not know the version as part of an active document."""
        ...


class VectorIndexRepository(Protocol):
    """Bound to one tenant for its whole life."""

    def add_version(self, record: IndexedVersion, chunks: Sequence[IndexedChunk]) -> bool:
        """Insert the version record and every vector atomically if the version has no record for
        this index. False when it already has one (never replaced)."""
        ...

    def get_version(self, version_id: VersionId, index_id: str) -> IndexedVersion | None: ...

    def versions_of(
        self, documents: Sequence[DocumentId]
    ) -> dict[DocumentId, tuple[VersionId, ...]]:
        """Every version of these documents that has any index record, whatever its index."""
        ...

    def nearest(
        self,
        index_id: str,
        documents: Sequence[DocumentId],
        query: Vector,
        limit: int,
        offset: int,
    ) -> tuple[Candidate, ...]:
        """Rows of exactly these documents and this index, most similar first, ties by chunk id."""
        ...

    def remove_all_for(self, document_id: DocumentId) -> int: ...

    def list_for(self, document_id: DocumentId) -> tuple[VersionId, ...]: ...

    def lock_document(self, document_id: DocumentId) -> None:
        """Serializes, until the transaction ends, every writer of this document's index rows with
        its purge: a row is never added after the purge's receipt, and the purge sees every row
        added before it."""
        ...


class ReceiptRepository(Protocol):
    def add(self, receipt: PurgeReceipt) -> bool:
        """Insert if absent. False when a receipt exists (receipts are never replaced)."""
        ...

    def get(self, document_id: DocumentId) -> PurgeReceipt | None: ...


class RetrievalUnitOfWork(UnitOfWork, Protocol):
    @property
    def index(self) -> VectorIndexRepository: ...

    @property
    def receipts(self) -> ReceiptRepository: ...

    @property
    def fence(self) -> JobFence: ...


class RetrievalUnitOfWorkFactory(Protocol):
    def begin(self, ctx: ExecutionContext) -> RetrievalUnitOfWork: ...
