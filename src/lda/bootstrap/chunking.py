"""Chunking wiring (ADR-038).

Order: `build_original_reader` and `build_version_state_reader` (bootstrap.documents) ->
`build_processing` -> `build_extraction_reader` -> `build_chunking(platform, reader, settings)` ->
`build_ingestion(..., stages=[processing.stage, chunking.stage])` -> `build_docs(...)`. The chunk
stage must come AFTER `extract` in the plan: it reads the extraction that stage records. Pass
`CHUNKING_MIGRATIONS` to `migrate.upgrade` and register `job_handlers` with the worker; the
`events` queue must be configured in `LDA_JOBS__QUEUES` (the purge handler is an `ASYNC_JOB` event
handler). Nothing here adds a scanner: the plan's mandatory `scan` stage stays ingestion's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lda.bootstrap.container import PlatformContainer, require_queues
from lda.modules.chunking.api import (
    ChunkingParameters,
    ChunkStage,
    PurgeChunks,
    PurgeChunksOnDocumentPurged,
    ReadChunks,
)
from lda.modules.chunking.config import ChunkingSettings
from lda.modules.chunking.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.chunking.infrastructure.persistence.unit_of_work import (
    SqlChunkingUnitOfWorkFactory,
)
from lda.modules.document_processing.api import ReadExtraction
from lda.modules.documents.api import DocumentPurged, ReadVersionState
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.events.job_handler import EventJobHandler
from lda.platform.events.registry import EVENT_QUEUE
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobHandler

CHUNKING_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class ChunkingContainer:
    stage: ChunkStage
    """Put it in the ingestion plan, after the `extract` stage."""
    purge_chunks: PurgeChunks
    job_handlers: Mapping[str, JobHandler]
    """Register with the job worker. The `events` queue must exist in `LDA_JOBS__QUEUES`."""


def load_chunking_settings(config: ConfigurationLoader) -> ChunkingSettings:
    return config.load("chunking", ChunkingSettings)


def build_chunking(
    platform: PlatformContainer, reader: ReadExtraction, settings: ChunkingSettings
) -> ChunkingContainer:
    require_queues(platform, EVENT_QUEUE)
    parameters = ChunkingParameters(
        settings.max_characters, settings.min_characters, settings.max_chunks
    )  # an unsafe pair fails here, at startup
    units_of_work = SqlChunkingUnitOfWorkFactory(platform.units_of_work)
    stage = ChunkStage(reader, parameters, units_of_work, platform.object_store, platform.clock)
    purge = PurgeChunks(units_of_work, platform.object_store, platform.clock)
    handler = PurgeChunksOnDocumentPurged(purge)
    platform.handlers.register(DocumentPurged, handler, Delivery.ASYNC_JOB)
    return ChunkingContainer(
        stage=stage,
        purge_chunks=purge,
        job_handlers={
            f"event:{handler.name}": EventJobHandler(
                handler, platform.event_codec, platform.units_of_work
            )
        },
    )


def build_chunk_reader(platform: PlatformContainer, versions: ReadVersionState) -> ReadChunks:
    """The only way a later module reads chunk text and chunk ids (ADR-008, Step 11): Documents is
    asked first, then the recorded, hash-checked artifact is read. Stateless, like
    `build_extraction_reader`; built before `build_index` (bootstrap.retrieval)."""
    return ReadChunks(
        SqlChunkingUnitOfWorkFactory(platform.units_of_work), platform.object_store, versions
    )
