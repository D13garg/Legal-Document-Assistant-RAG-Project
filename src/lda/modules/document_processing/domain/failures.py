"""Why a document cannot be processed. Stable, content-free identifiers (ADR-010 §4 error codes)."""

from __future__ import annotations

from lda.shared.kernel.errors import DomainError

UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
MALFORMED_DOCUMENT = "malformed_document"
ENCRYPTED_DOCUMENT = "encrypted_document"
EMPTY_DOCUMENT = "empty_document"
NO_EXTRACTABLE_TEXT = "no_extractable_text"
LIMIT_BYTES = "limit_exceeded.bytes"
LIMIT_EXPANDED_BYTES = "limit_exceeded.expanded_bytes"
LIMIT_PAGES = "limit_exceeded.pages"
LIMIT_CHARACTERS = "limit_exceeded.characters"
VERSION_UNAVAILABLE = "version_unavailable"
ORIGINAL_MISSING = "original_missing"
ARTIFACT_CONFLICT = "artifact_conflict"
ARTIFACT_UNRECOVERABLE = "artifact_unrecoverable"

PERMANENT_CODES = frozenset(
    {
        UNSUPPORTED_MEDIA_TYPE,
        MALFORMED_DOCUMENT,
        ENCRYPTED_DOCUMENT,
        EMPTY_DOCUMENT,
        NO_EXTRACTABLE_TEXT,
        LIMIT_BYTES,
        LIMIT_EXPANDED_BYTES,
        LIMIT_PAGES,
        LIMIT_CHARACTERS,
        VERSION_UNAVAILABLE,
        ORIGINAL_MISSING,
        ARTIFACT_CONFLICT,
        ARTIFACT_UNRECOVERABLE,
    }
)


class ExtractionRejected(DomainError):
    """This content can never be extracted (retrying cannot help). `code` is one of the above."""

    def __init__(self, code: str) -> None:
        if code not in PERMANENT_CODES:
            raise ValueError("unknown extraction failure code")
        super().__init__(code)
