"""What Postgres knows about a chunk set: identifiers, hashes, counts. Never the text (ADR-038)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from lda.modules.chunking.domain.chunk import HASH_PATTERN, ChunkSet, content_hash
from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import TenantId

PURGE_SCOPE = "chunking.chunk_artifacts_and_rows"
"""All a receipt claims: the chunk artifacts and this module's rows are gone. Nothing about the
originals, extractions, embeddings, indexes or backups (ADR-023)."""

VERSION_PATTERN = re.compile(r"[A-Za-z0-9._+/ ;=-]{1,200}")


@dataclass(frozen=True, slots=True)
class ChunkSetRecord:
    tenant_id: TenantId
    document_id: DocumentId
    version_id: VersionId
    extraction_hash: str
    chunker_version: str
    content_hash: str
    artifact_bytes: int
    chunk_count: int
    character_count: int
    created_at: datetime

    def __post_init__(self) -> None:
        if HASH_PATTERN.fullmatch(self.content_hash) is None:
            raise ValidationError("chunk_record_hash_invalid")
        if HASH_PATTERN.fullmatch(self.extraction_hash) is None:
            raise ValidationError("chunk_record_hash_invalid")
        if VERSION_PATTERN.fullmatch(self.chunker_version) is None:
            raise ValidationError("chunk_record_chunker_invalid")
        if min(self.artifact_bytes, self.chunk_count, self.character_count) < 1:
            raise ValidationError("chunk_record_counts_invalid")
        if self.created_at.utcoffset() is None:
            raise ValidationError("chunk_record_time_naive")

    @classmethod
    def describe(cls, chunk_set: ChunkSet, now: datetime) -> tuple[ChunkSetRecord, bytes]:
        """The record for `chunk_set` and the canonical artifact bytes it describes."""
        artifact = chunk_set.to_bytes()
        record = cls(
            chunk_set.tenant_id,
            chunk_set.document_id,
            chunk_set.version_id,
            chunk_set.extraction_hash,
            chunk_set.chunker_version,
            content_hash(artifact),
            len(artifact),
            len(chunk_set.chunks),
            chunk_set.character_count,
            now,
        )
        return record, artifact


class Reconciliation(StrEnum):
    """What to do when a version already has a recorded chunk set (immutable output, ADR-038)."""

    ADOPT = "adopt"  # the recorded artifact is exactly what this run produced
    KEEP_RECORDED = "keep_recorded"  # produced by another chunker version: kept, never replaced
    CONFLICT = "conflict"  # same chunker version, different output: refuse, never overwrite


def reconcile(recorded: ChunkSetRecord, produced: ChunkSetRecord) -> Reconciliation:
    if recorded.chunker_version != produced.chunker_version:
        return Reconciliation.KEEP_RECORDED
    if recorded.content_hash != produced.content_hash:
        return Reconciliation.CONFLICT
    return Reconciliation.ADOPT


@dataclass(frozen=True, slots=True)
class PurgeReceipt:
    tenant_id: TenantId
    document_id: DocumentId
    purged_at: datetime
    version_ids: tuple[VersionId, ...]
    scope: str = PURGE_SCOPE

    @property
    def versions_purged(self) -> int:
        return len(self.version_ids)
