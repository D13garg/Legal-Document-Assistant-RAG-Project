"""Where the derived artifact lives (ADR-037). Only this module writes or deletes these keys."""

from __future__ import annotations

from lda.modules.document_processing.domain.ids import DocumentId, VersionId


def extracted_path(document_id: DocumentId, version_id: VersionId) -> str:
    """Object key of a version's extracted artifact, relative to the tenant prefix."""
    return f"document_processing/{document_id}/{version_id}/extracted"
