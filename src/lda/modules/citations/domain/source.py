"""`CitationSource`: a reference to one chunk of one immutable version (ADR-016).

It is a reference, never an excerpt: no text, no section titles, no offsets (chunking records none,
so none are invented). Pages are optional and appear only as the chunk store recorded them. A
source is not an authorization: showing or reading what it points at needs a fresh decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from lda.modules.citations.domain.codes import INVALID_INPUT
from lda.modules.citations.domain.ids import CitedDocumentId, CitedVersionId
from lda.shared.kernel.errors import ValidationError

DOCUMENT_KIND = "document"
"""Same value as `documents.api.DOCUMENT_KIND`; a pin in tests/architecture checks equality."""
READ_DOCUMENT = "documents.read_document"
"""Same value as `documents.api.READ_DOCUMENT`; a test pins equality."""
MAX_PAGE = 1_000_000
MAX_ORDINAL = 1_000_000
_DIGEST = re.compile(r"[0-9a-f]{64}")


def digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValidationError(INVALID_INPUT, detail=field)
    return value


def _page(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PAGE:
        raise ValidationError(INVALID_INPUT, detail=field)
    return value


@dataclass(frozen=True, slots=True)
class CitationSource:
    document_id: CitedDocumentId
    version_id: CitedVersionId
    chunk_id: UUID
    ordinal: int
    text_hash: str
    page_start: int | None = None
    page_end: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, CitedDocumentId) or not isinstance(
            self.version_id, CitedVersionId
        ):
            raise ValidationError(INVALID_INPUT, detail="source_ids")
        if not isinstance(self.chunk_id, UUID):
            raise ValidationError(INVALID_INPUT, detail="chunk_id")
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or not 0 <= self.ordinal <= MAX_ORDINAL
        ):
            raise ValidationError(INVALID_INPUT, detail="ordinal")
        digest(self.text_hash, "text_hash")
        start, end = _page(self.page_start, "page_start"), _page(self.page_end, "page_end")
        if (start is None) != (end is None) or (
            start is not None and end is not None and start > end
        ):
            raise ValidationError(INVALID_INPUT, detail="pages")
