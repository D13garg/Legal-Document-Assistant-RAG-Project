"""One chunk, its explicit boundaries, and the immutable chunk set of a version.

A chunk set is content: it lives only in the object store, never in Postgres, an event or a log
(ADR-038). It is canonical: the same extraction and the same chunker version always give the same
bytes, which is what makes a re-run after a crash provably idempotent and an answer's provenance
reproducible.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from uuid import UUID, uuid5

from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import TenantId

SCHEMA_VERSION = 1
SEPARATOR = "\n\n"
"""Joins the segments of one chunk. A chunk's text is exactly its segments' source text, in order,
joined by this separator: a reader can rebuild and verify it from the extraction."""

HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
CHUNK_NAMESPACE = UUID("6c2f3a52-8a53-5b0e-9f7e-3d1c4b6a7e10")


def content_hash(artifact: bytes) -> str:
    return hashlib.sha256(artifact).hexdigest()


@dataclass(frozen=True, slots=True)
class Segment:
    """A span of one extracted block: code points `[start, end)` of block `block_index`."""

    block_index: int
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.block_index < 0 or not 0 <= self.start < self.end:
            raise ValidationError("chunk_segment_invalid")


@dataclass(frozen=True, slots=True)
class Chunk:
    """`section_path` is the heading trail in effect where the chunk begins (outermost first, each
    label cut to a bounded length). Pages are 1-based and only present for paged formats."""

    ordinal: int
    text: str = field(repr=False)
    segments: tuple[Segment, ...]
    section_path: tuple[str, ...]
    page_start: int | None
    page_end: int | None

    def __post_init__(self) -> None:
        if self.ordinal < 0 or not self.text or not self.segments:
            raise ValidationError("chunk_invalid")
        if (self.page_start is None) != (self.page_end is None):
            raise ValidationError("chunk_pages_invalid")
        if self.page_start is not None and not 1 <= self.page_start <= (self.page_end or 0):
            raise ValidationError("chunk_pages_invalid")

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkSet:
    """All chunks of one version, derived from one extraction by one chunker version."""

    tenant_id: TenantId
    document_id: DocumentId
    version_id: VersionId
    extraction_hash: str
    chunker_version: str
    chunks: tuple[Chunk, ...]

    def __post_init__(self) -> None:
        if HASH_PATTERN.fullmatch(self.extraction_hash) is None:
            raise ValidationError("chunk_set_hash_invalid")
        if not self.chunks or [c.ordinal for c in self.chunks] != list(range(len(self.chunks))):
            raise ValidationError("chunk_set_ordinals_invalid")

    @property
    def character_count(self) -> int:
        return sum(len(chunk.text) for chunk in self.chunks)

    def chunk_id(self, ordinal: int) -> UUID:
        """Stable and unique per tenant, version, extraction, chunker version and position."""
        name = (
            f"{self.tenant_id}/{self.version_id}/{self.extraction_hash}"
            f"/{self.chunker_version}/{ordinal}"
        )
        return uuid5(CHUNK_NAMESPACE, name)

    def to_bytes(self) -> bytes:
        """Canonical JSON: sorted keys, no whitespace, ASCII only."""
        payload: dict[str, object] = {
            "schema": SCHEMA_VERSION,
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "extraction_hash": self.extraction_hash,
            "chunker_version": self.chunker_version,
            "chunks": [
                {
                    "ordinal": c.ordinal,
                    "id": str(self.chunk_id(c.ordinal)),
                    "text": c.text,
                    "text_hash": c.text_hash,
                    "char_count": len(c.text),
                    "section_path": list(c.section_path),
                    "page_start": c.page_start,
                    "page_end": c.page_end,
                    "segments": [[s.block_index, s.start, s.end] for s in c.segments],
                }
                for c in self.chunks
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
