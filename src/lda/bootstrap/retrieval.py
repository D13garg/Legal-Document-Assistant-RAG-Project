"""Retrieval wiring (ADR-004, ADR-007, ADR-008).

Order: `build_version_state_reader` (bootstrap.documents) -> `build_chunk_reader`
(bootstrap.chunking) -> `build_index(platform, ...)` ->
`build_ingestion(..., stages=[processing.stage, chunking.stage, retrieval.stage])`. The index stage
must come AFTER `chunk`: it reads the chunk set that stage records. Because the highest `ready`
version of a document is the only one searched (ADR-008), every version must pass the `index` stage
before it becomes `ready`: a plan without it must not be run next to `SearchIndex`. Pass
`RETRIEVAL_MIGRATIONS` to `migrate.upgrade` (needs a role allowed to create the pgvector extension)
and register `job_handlers` with the worker; the `events` queue must be configured in
`LDA_JOBS__QUEUES`. The embedder is passed in: only the deterministic fake exists, and a real one
needs an approved embedding-egress rule first (ADR-007). The `verifier` is
`build_access_control(...).proof_verifier`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lda.bootstrap.container import PlatformContainer, require_queues
from lda.modules.chunking.api import ReadChunks
from lda.modules.documents.api import DocumentPurged, ReadVersionState
from lda.modules.retrieval.api import (
    DevelopmentEmbedder,
    IndexStage,
    PurgeRetrievalIndex,
    PurgeRetrievalIndexOnDocumentPurged,
    SearchIndex,
    SearchLimits,
)
from lda.modules.retrieval.api import (
    Embedder as Embedder,  # explicit re-export: used by bootstrap/worker.py
)
from lda.modules.retrieval.config import RetrievalSettings
from lda.modules.retrieval.infrastructure.gateways import (
    ChunkingChunkSource,
    DocumentsVersionStates,
)
from lda.modules.retrieval.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.retrieval.infrastructure.persistence.unit_of_work import (
    SqlRetrievalUnitOfWorkFactory,
)
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.events.job_handler import EventJobHandler
from lda.platform.events.registry import EVENT_QUEUE
from lda.shared.ports.authorization import ProofVerifier
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobHandler

RETRIEVAL_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class IndexContainer:
    stage: IndexStage
    """Put it in the ingestion plan, after the `chunk` stage."""
    search: SearchIndex
    purge_index: PurgeRetrievalIndex
    job_handlers: Mapping[str, JobHandler]
    """Register with the job worker. The `events` queue must exist in `LDA_JOBS__QUEUES`."""


def load_index_settings(config: ConfigurationLoader) -> RetrievalSettings:
    return config.load("retrieval", RetrievalSettings)


def build_index(
    platform: PlatformContainer,
    *,
    chunks: ReadChunks,
    versions: ReadVersionState,
    verifier: ProofVerifier,
    embedder: Embedder,
    settings: RetrievalSettings,
) -> IndexContainer:
    require_queues(platform, EVENT_QUEUE)
    limits = SearchLimits(
        settings.max_results,
        settings.max_query_characters,
        settings.candidate_pool,
        settings.max_candidates,
    )  # an unsafe combination fails here, at startup
    units_of_work = SqlRetrievalUnitOfWorkFactory(platform.units_of_work)
    stage = IndexStage(
        ChunkingChunkSource(chunks),
        embedder,
        units_of_work,
        platform.clock,
        settings.embedding_batch_size,
    )
    search = SearchIndex(
        units_of_work, embedder, verifier, DocumentsVersionStates(versions), limits
    )
    purge = PurgeRetrievalIndex(units_of_work, platform.clock)
    handler = PurgeRetrievalIndexOnDocumentPurged(purge)
    platform.handlers.register(DocumentPurged, handler, Delivery.ASYNC_JOB)
    return IndexContainer(
        stage=stage,
        search=search,
        purge_index=purge,
        job_handlers={
            f"event:{handler.name}": EventJobHandler(
                handler, platform.event_codec, platform.units_of_work
            )
        },
    )


def build_development_embedder() -> Embedder:
    """The offline DEVELOPMENT embedder (ADR-007 amendment, ADR-045 D3). Call only through
    `lda.bootstrap.development`, which requires the explicit development guard first. There is
    no default and no production selection of it."""
    return DevelopmentEmbedder()
