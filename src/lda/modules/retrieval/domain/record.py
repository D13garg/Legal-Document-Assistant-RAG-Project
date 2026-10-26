"""What Postgres knows of the index: identifiers, hashes, counts, vectors. Never text (ADR-038)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from lda.modules.retrieval.domain.embedding import (
    INDEX_ID_PATTERN,
    EmbeddingIdentity,
    Vector,
)
from lda.modules.retrieval.domain.ids import ChunkId, DocumentId, VersionId
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import TenantId

PURGE_SCOPE = "retrieval.vector_rows"
"""All a receipt claims: this module's vector rows and version records are gone. Nothing about the
originals, extractions, chunk artifacts, backups or replicas (ADR-023)."""

HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
CHUNKER_PATTERN = re.compile(r"[A-Za-z0-9._+/ ;=-]{1,200}")


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    chunk_id: ChunkId
    ordinal: int
    text_hash: str
    vector: Vector = field(repr=False)

    def __post_init__(self) -> None:
        if self.ordinal < 0 or HASH_PATTERN.fullmatch(self.text_hash) is None:
            raise ValidationError("index_chunk_invalid")


@dataclass(frozen=True, slots=True)
class IndexedVersion:
    tenant_id: TenantId
    document_id: DocumentId
    version_id: VersionId
    embedding: EmbeddingIdentity
    chunker_version: str
    source_hash: str
    chunk_count: int
    created_at: datetime

    def __post_init__(self) -> None:
        if HASH_PATTERN.fullmatch(self.source_hash) is None:
            raise ValidationError("index_record_hash_invalid")
        if CHUNKER_PATTERN.fullmatch(self.chunker_version) is None:
            raise ValidationError("index_record_chunker_invalid")
        if self.chunk_count < 1:
            raise ValidationError("index_record_counts_invalid")
        if self.created_at.utcoffset() is None:
            raise ValidationError("index_record_time_naive")

    @property
    def index_id(self) -> str:
        return self.embedding.index_id


@dataclass(frozen=True, slots=True)
class PurgeReceipt:
    tenant_id: TenantId
    document_id: DocumentId
    purged_at: datetime
    version_ids: tuple[VersionId, ...]
    scope: str = PURGE_SCOPE

    def __post_init__(self) -> None:
        if self.scope != PURGE_SCOPE or self.purged_at.utcoffset() is None:
            raise ValidationError("index_receipt_invalid")


def valid_index_id(value: str) -> bool:
    return INDEX_ID_PATTERN.fullmatch(value) is not None
