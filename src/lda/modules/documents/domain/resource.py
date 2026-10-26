"""The names `documents` contributes to Access Control and to object storage (ADR-014, ADR-005)."""

from __future__ import annotations

from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId

DOCUMENT_KIND = "document"
READ_DOCUMENT = "documents.read_document"
DELETE_DOCUMENT = "documents.delete_document"
ADD_DOCUMENT_VERSION = "documents.add_document_version"
DOCUMENT_ACTIONS = (READ_DOCUMENT, DELETE_DOCUMENT, ADD_DOCUMENT_VERSION)


def original_path(document_id: DocumentId, version_id: DocumentVersionId) -> str:
    """Object key of a version's original, relative to the tenant prefix. Identifiers only."""
    return f"documents/{document_id}/{version_id}/original"
