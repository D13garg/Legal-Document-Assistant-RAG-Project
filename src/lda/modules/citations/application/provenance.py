"""`ChunkProvenanceReader` over chunking's public `ReadChunks`: text is read and dropped at once."""

from __future__ import annotations

from lda.modules.chunking.api import DocumentId, ReadChunks, ReadChunksCommand, VersionId
from lda.modules.citations.application.ports import ChunkProvenance
from lda.modules.citations.domain.ids import CitedDocumentId, CitedVersionId
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.ids import TenantId

PURPOSE = "citations.verify_provenance"


class ReadChunksProvenance:
    def __init__(self, reader: ReadChunks) -> None:
        self._reader = reader

    def read(
        self, tenant_id: TenantId, document_id: CitedDocumentId, version_id: CitedVersionId
    ) -> tuple[ChunkProvenance, ...]:
        stored = self._reader.execute(
            SystemContext(tenant_id, PURPOSE),
            ReadChunksCommand(DocumentId(document_id.value), VersionId(version_id.value)),
        )
        return tuple(
            ChunkProvenance(c.chunk_id, c.ordinal, c.text_hash, c.page_start, c.page_end)
            for c in stored.chunks
        )
