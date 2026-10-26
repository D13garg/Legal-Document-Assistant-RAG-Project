"""Request and result of `ReadChunks` (Step 11, ADR-038 amendment, ADR-008).

Chunk text is content: it is `repr=False` everywhere and never reaches Postgres, events, audit,
jobs or logs. The result goes to a system caller only; serving text to a user or a prompt is
another module's decision and needs an authorization decision first (ADR-014).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from lda.modules.chunking.domain.ids import DocumentId, VersionId


@dataclass(frozen=True, slots=True)
class ReadChunksCommand:
    document_id: DocumentId
    version_id: VersionId


@dataclass(frozen=True, slots=True)
class ChunkView:
    chunk_id: UUID
    ordinal: int
    text: str = field(repr=False)
    text_hash: str
    section_path: tuple[str, ...] = field(repr=False)
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True, slots=True)
class StoredChunks:
    document_id: DocumentId
    version_id: VersionId
    extraction_hash: str
    chunker_version: str
    content_hash: str
    chunks: tuple[ChunkView, ...] = field(repr=False)
