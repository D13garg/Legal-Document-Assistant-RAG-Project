from __future__ import annotations

from dataclasses import dataclass, field

from lda.modules.document_processing.domain.extraction import ExtractedDocument
from lda.modules.document_processing.domain.ids import DocumentId, VersionId


@dataclass(frozen=True, slots=True)
class PurgeCommand:
    document_id: DocumentId


@dataclass(frozen=True, slots=True)
class PurgeView:
    document_id: DocumentId
    purged: bool
    versions_purged: int


@dataclass(frozen=True, slots=True)
class ReadExtractionCommand:
    document_id: DocumentId
    version_id: VersionId


@dataclass(frozen=True, slots=True)
class StoredExtraction:
    """The recorded extraction of one version. Content: the text never appears in a repr, a log,
    an event or an audit fact. `content_hash` and `parser_version` identify exactly what was read,
    so a consumer can record the provenance of what it derives."""

    document_id: DocumentId
    version_id: VersionId
    parser_version: str
    content_hash: str
    extracted: ExtractedDocument = field(repr=False)
