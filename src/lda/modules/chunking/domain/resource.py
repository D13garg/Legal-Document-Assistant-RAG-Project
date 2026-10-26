"""Where the derived artifact lives (ADR-038). Only this module writes or deletes these keys."""

from __future__ import annotations

from lda.modules.chunking.domain.ids import DocumentId, VersionId


def chunks_path(document_id: DocumentId, version_id: VersionId) -> str:
    """Object key of a version's chunk set, relative to the tenant prefix."""
    return f"chunking/{document_id}/{version_id}/chunks"
