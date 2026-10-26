from __future__ import annotations

from dataclasses import dataclass

from lda.modules.chunking.domain.ids import DocumentId


@dataclass(frozen=True, slots=True)
class PurgeCommand:
    document_id: DocumentId


@dataclass(frozen=True, slots=True)
class PurgeView:
    document_id: DocumentId
    purged: bool
    versions_purged: int
