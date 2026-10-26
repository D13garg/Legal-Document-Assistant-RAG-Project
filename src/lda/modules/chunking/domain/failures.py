"""Why a version cannot be chunked. Stable, content-free identifiers (ADR-010 §4 error codes)."""

from __future__ import annotations

from lda.shared.kernel.errors import DomainError

NO_CHUNKABLE_TEXT = "no_chunkable_text"
LIMIT_CHUNKS = "limit_exceeded.chunks"
VERSION_UNAVAILABLE = "version_unavailable"
EXTRACTION_UNAVAILABLE = "extraction_unavailable"
EXTRACTION_CORRUPT = "extraction_corrupt"
ARTIFACT_CONFLICT = "artifact_conflict"
ARTIFACT_UNRECOVERABLE = "artifact_unrecoverable"

PERMANENT_CODES = frozenset(
    {
        NO_CHUNKABLE_TEXT,
        LIMIT_CHUNKS,
        VERSION_UNAVAILABLE,
        EXTRACTION_UNAVAILABLE,
        EXTRACTION_CORRUPT,
        ARTIFACT_CONFLICT,
        ARTIFACT_UNRECOVERABLE,
    }
)


class ChunkingRejected(DomainError):
    """This text can never be chunked (retrying cannot help). `code` is one of the above."""

    def __init__(self, code: str) -> None:
        if code not in PERMANENT_CODES:
            raise ValueError("unknown chunking failure code")
        super().__init__(code)
