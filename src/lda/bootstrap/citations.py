"""Citations wiring (ADR-016): joins the evidence builder and recorder to settings and ports.

Records go to the `GenerationRecordRepository` port. Production passes `build_generation_records`
(Postgres, insert-only, forced RLS; pass `CITATIONS_MIGRATIONS` to `migrate.upgrade` to create the
`citations` schema); tests may pass the in-memory twin. The proof verifier and the chunk reader are
injected by the caller; the builder verifies the proof before any chunk is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from lda.bootstrap.container import PlatformContainer
from lda.modules.chunking.api import ReadChunks
from lda.modules.citations.api import (
    ChunkContent as ChunkContent,
)
from lda.modules.citations.api import (
    ChunkContentReader as ChunkContentReader,
)
from lda.modules.citations.api import (
    CitationLimits,
    CitedDocumentId,
    CitedVersionId,
    EvidenceBuilder,
    GenerationRecorder,
    GenerationRecordRepository,
    ReadChunksContent,
    ReadChunksProvenance,
)
from lda.modules.citations.config import CitationsSettings
from lda.modules.citations.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.citations.infrastructure.persistence.unit_of_work import (
    UnitOfWorkGenerationRecords,
)
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.authorization import ProofVerifier
from lda.shared.ports.clock import Clock, IdGenerator

CITATIONS_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class EvidenceContainer:
    evidence_builder: EvidenceBuilder
    generation_recorder: GenerationRecorder
    max_markers: int
    """Pass to `EvidenceSet.resolve(..., max_markers=...)`."""


def load_evidence_settings(config: ConfigurationLoader) -> CitationsSettings:
    return config.load("citations", CitationsSettings)


def build_generation_records(platform: PlatformContainer) -> GenerationRecordRepository:
    return UnitOfWorkGenerationRecords(platform.units_of_work)


def build_evidence(
    settings: CitationsSettings,
    chunk_reader: ReadChunks,
    verifier: ProofVerifier,
    records: GenerationRecordRepository,
    clock: Clock,
    ids: IdGenerator,
) -> EvidenceContainer:
    limits = CitationLimits(settings.max_sources, settings.max_markers)
    return EvidenceContainer(
        EvidenceBuilder(ReadChunksProvenance(chunk_reader), verifier, limits),
        GenerationRecorder(records, clock, ids),
        settings.max_markers,
    )


def build_chunk_content_reader(chunks: ReadChunks) -> ChunkContentReader:
    """The authorized-evidence content reader that grounded generation uses (ADR-045 reuses it
    so the headless driver verifies citations through the same interface)."""
    return ReadChunksContent(chunks)


def read_cited_chunks(
    reader: ChunkContentReader, tenant_id: TenantId, resource: UUID, revision: UUID
) -> tuple[ChunkContent, ...]:
    """Stored chunk text and hashes for one cited version; used to check citations resolve."""
    return reader.read(tenant_id, CitedDocumentId(resource), CitedVersionId(revision))
