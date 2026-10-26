"""Ports of citations (ADR-016): chunk provenance in, generation records out."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from lda.modules.citations.domain.ids import CitedDocumentId, CitedVersionId
from lda.modules.citations.domain.record import GenerationRecord
from lda.shared.kernel.ids import TenantId


@dataclass(frozen=True, slots=True)
class ChunkProvenance:
    """What the chunk store recorded about one chunk. No text, by construction."""

    chunk_id: UUID
    ordinal: int
    text_hash: str
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True, slots=True)
class ChunkContent(ChunkProvenance):
    """Provenance plus the stored chunk text (ADR-016 amendment, Step 14). The text is content:
    `repr=False`, and it exists only so `TextEvidenceBuilder` can verify and hand it on."""

    text: str = field(repr=False)


class ChunkProvenanceReader(Protocol):
    def read(
        self, tenant_id: TenantId, document_id: CitedDocumentId, version_id: CitedVersionId
    ) -> tuple[ChunkProvenance, ...]:
        """The chunks of one version under `tenant_id`.

        Raises `NotFoundError`/`ConflictError` when the version is unknown, foreign, deleted,
        purged or not eligible; the builder maps both to one stable code. It must not return, log
        or retain chunk text.
        """
        ...


class ChunkContentReader(Protocol):
    def read(
        self, tenant_id: TenantId, document_id: CitedDocumentId, version_id: CitedVersionId
    ) -> tuple[ChunkContent, ...]:
        """The chunks of one version under `tenant_id`, with their text.

        Same refusals as `ChunkProvenanceReader`. The caller has been authorized before this is
        called (`TextEvidenceBuilder` verifies the proof first); the reader must not log or retain
        the text and must read through chunking's public API only.
        """
        ...


class GenerationRecordRepository(Protocol):
    """Insert-only by contract: there is deliberately no update and no delete."""

    def add(self, record: GenerationRecord) -> None:
        """Raises `ConflictError(citations_record_exists)` if the id is taken."""
        ...

    def get(self, tenant_id: TenantId, record_id: UUID) -> GenerationRecord | None:
        """Tenant-bound: another tenant's record is `None`."""
        ...
