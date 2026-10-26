"""What Postgres knows about an extraction: identifiers, hash, counts. Never the text (ADR-037)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from lda.modules.document_processing.domain.extraction import ExtractedDocument, content_hash
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import TenantId

PURGE_SCOPE = "document_processing.extracted_artifacts_and_rows"
"""All a receipt claims: the extracted artifacts and this module's rows are gone. Nothing about the
originals, other modules' derived data (chunks, indexes) or backups (ADR-023)."""

HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
PARSER_VERSION_PATTERN = re.compile(r"[A-Za-z0-9._+/ ;=-]{1,200}")


@dataclass(frozen=True, slots=True)
class ExtractionRecord:
    tenant_id: TenantId
    document_id: DocumentId
    version_id: VersionId
    parser_version: str
    content_hash: str
    media_type: str
    artifact_bytes: int
    block_count: int
    character_count: int
    page_count: int | None
    created_at: datetime

    def __post_init__(self) -> None:
        if HASH_PATTERN.fullmatch(self.content_hash) is None:
            raise ValidationError("extraction_record_hash_invalid")
        if PARSER_VERSION_PATTERN.fullmatch(self.parser_version) is None:
            raise ValidationError("extraction_record_parser_invalid")
        if min(self.artifact_bytes, self.block_count, self.character_count) < 1:
            raise ValidationError("extraction_record_counts_invalid")
        if self.page_count is not None and self.page_count < 1:
            raise ValidationError("extraction_record_counts_invalid")
        if self.created_at.utcoffset() is None:
            raise ValidationError("extraction_record_time_naive")

    @classmethod
    def describe(
        cls,
        *,
        tenant_id: TenantId,
        document_id: DocumentId,
        version_id: VersionId,
        parser_version: str,
        extracted: ExtractedDocument,
        now: datetime,
    ) -> tuple[ExtractionRecord, bytes]:
        """The record for `extracted` and the canonical artifact bytes it describes."""
        artifact = extracted.to_bytes()
        record = cls(
            tenant_id,
            document_id,
            version_id,
            parser_version,
            content_hash(artifact),
            extracted.media_type,
            len(artifact),
            len(extracted.blocks),
            extracted.character_count,
            extracted.page_count,
            now,
        )
        return record, artifact


class Reconciliation(StrEnum):
    """What to do when a version already has a recorded extraction (immutable output, ADR-037)."""

    ADOPT = "adopt"  # the recorded artifact is exactly what this run produced
    KEEP_RECORDED = "keep_recorded"  # produced by another parser version: kept, never replaced
    CONFLICT = "conflict"  # same parser version, different output: refuse, never overwrite


def reconcile(recorded: ExtractionRecord, produced: ExtractionRecord) -> Reconciliation:
    if recorded.parser_version != produced.parser_version:
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
