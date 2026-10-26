"""Records that exist only to make cleanup and deletion provable (ADR-005, ADR-023)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.shared.kernel.ids import TenantId

PURGE_SCOPE = "documents.originals_and_rows"
"""All a receipt claims: the originals and the Documents rows are gone. Nothing about any other
module's data (chunks, embeddings, search projections, caches) or about backups (ADR-023)."""


@dataclass(frozen=True, slots=True)
class UploadIntent:
    """Written before the first byte of an upload; the handle that finds an orphan (ADR-005)."""

    tenant_id: TenantId
    version_id: DocumentVersionId
    document_id: DocumentId
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PurgeReceipt:
    tenant_id: TenantId
    document_id: DocumentId
    tombstoned_at: datetime
    purged_at: datetime
    version_ids: tuple[DocumentVersionId, ...]
    scope: str = PURGE_SCOPE

    @property
    def versions_purged(self) -> int:
        return len(self.version_ids)
