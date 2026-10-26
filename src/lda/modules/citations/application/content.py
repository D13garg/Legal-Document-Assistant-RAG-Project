"""`ChunkContentReader` over chunking's public `ReadChunks`: the single place citations reads text.

Chunk-text access stays centralized in `ReadChunks` (ADR-008, ADR-016 amendment): this adapter adds
no second path, it keeps the text that `ReadChunksProvenance` drops.
"""

from __future__ import annotations

from lda.modules.chunking.api import DocumentId, ReadChunks, ReadChunksCommand, VersionId
from lda.modules.citations.application.ports import ChunkContent
from lda.modules.citations.domain.ids import CitedDocumentId, CitedVersionId
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.ids import TenantId

PURPOSE = "citations.read_evidence_text"


class ReadChunksContent:
    def __init__(self, reader: ReadChunks) -> None:
        self._reader = reader

    def read(
        self, tenant_id: TenantId, document_id: CitedDocumentId, version_id: CitedVersionId
    ) -> tuple[ChunkContent, ...]:
        stored = self._reader.execute(
            SystemContext(tenant_id, PURPOSE),
            ReadChunksCommand(DocumentId(document_id.value), VersionId(version_id.value)),
        )
        return tuple(
            ChunkContent(c.chunk_id, c.ordinal, c.text_hash, c.page_start, c.page_end, c.text)
            for c in stored.chunks
        )
