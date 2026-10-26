"""Why a version cannot be indexed. Stable, content-free identifiers (ADR-010 §4 error codes)."""

from __future__ import annotations

from lda.shared.kernel.errors import DomainError

VERSION_UNAVAILABLE = "version_unavailable"
CHUNKS_UNAVAILABLE = "chunks_unavailable"
CHUNKS_CORRUPT = "chunks_corrupt"
EMBEDDING_INVALID = "embedding_invalid"
INDEX_CONFLICT = "index_conflict"

PERMANENT_CODES = frozenset(
    {VERSION_UNAVAILABLE, CHUNKS_UNAVAILABLE, CHUNKS_CORRUPT, EMBEDDING_INVALID, INDEX_CONFLICT}
)


class RetrievalRejected(DomainError):
    """This version can never be indexed as it is (retrying cannot help). `code` is one above."""

    def __init__(self, code: str) -> None:
        if code not in PERMANENT_CODES:
            raise ValueError("unknown retrieval failure code")
        super().__init__(code)
