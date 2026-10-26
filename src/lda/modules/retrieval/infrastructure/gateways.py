"""Adapters over other modules' public APIs. Nothing here touches another module's storage."""

from __future__ import annotations

from lda.modules.chunking.api import (
    CHUNKS_ARTIFACT_CORRUPT,
    CHUNKS_ARTIFACT_MISSING,
    CHUNKS_NOT_FOUND,
    ReadChunks,
    ReadChunksCommand,
)
from lda.modules.chunking.api import DocumentId as ChunkDocumentId
from lda.modules.chunking.api import VersionId as ChunkVersionId
from lda.modules.documents.api import DocumentId as SourceDocumentId
from lda.modules.documents.api import DocumentVersionId as SourceVersionId
from lda.modules.documents.api import (
    ReadVersionState,
    VersionStateQuery,
    VersionStatus,
)
from lda.modules.retrieval.application.source import SourceChunk, SourceChunks
from lda.modules.retrieval.domain.failures import (
    CHUNKS_CORRUPT,
    CHUNKS_UNAVAILABLE,
    VERSION_UNAVAILABLE,
    RetrievalRejected,
)
from lda.modules.retrieval.domain.ids import ChunkId, DocumentId, VersionId
from lda.modules.retrieval.domain.selection import VersionFact
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ConflictError, NotFoundError

_UNAVAILABLE = frozenset({CHUNKS_NOT_FOUND, CHUNKS_ARTIFACT_MISSING})


class ChunkingChunkSource:
    """`ChunkSource` over `chunking.api.ReadChunks`, which asks Documents about eligibility."""

    def __init__(self, reader: ReadChunks) -> None:
        self._reader = reader

    def read(
        self, ctx: SystemContext, document_id: DocumentId, version_id: VersionId
    ) -> SourceChunks:
        try:
            stored = self._reader.execute(
                ctx,
                ReadChunksCommand(
                    ChunkDocumentId(document_id.value), ChunkVersionId(version_id.value)
                ),
            )
        except NotFoundError as exc:
            raise RetrievalRejected(
                CHUNKS_UNAVAILABLE if exc.code in _UNAVAILABLE else VERSION_UNAVAILABLE
            ) from None
        except ConflictError as exc:
            raise RetrievalRejected(
                CHUNKS_CORRUPT if exc.code == CHUNKS_ARTIFACT_CORRUPT else VERSION_UNAVAILABLE
            ) from None
        return SourceChunks(
            stored.chunker_version,
            stored.content_hash,
            tuple(
                SourceChunk(ChunkId(c.chunk_id), c.ordinal, c.text, c.text_hash)
                for c in stored.chunks
            ),
        )


class DocumentsVersionStates:
    """`VersionStates` over `documents.api.ReadVersionState`: status and number, live, per call."""

    def __init__(self, reader: ReadVersionState) -> None:
        self._reader = reader

    def read(
        self, ctx: SystemContext, document_id: DocumentId, version_id: VersionId
    ) -> VersionFact | None:
        try:
            view = self._reader.execute(
                ctx,
                VersionStateQuery(
                    SourceDocumentId(document_id.value), SourceVersionId(version_id.value)
                ),
            )
        except NotFoundError:
            return None
        return VersionFact(
            VersionId(view.version_id.value), view.number, view.status is VersionStatus.READY
        )
