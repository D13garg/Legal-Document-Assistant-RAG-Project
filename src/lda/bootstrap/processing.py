"""Document processing wiring (ADR-037).

Order: `build_original_reader(...)` (bootstrap.documents) -> `build_processing(platform, reader,
settings)` -> `build_ingestion(..., stages=[processing.stage, ...])` -> `build_docs(...)`. Ingestion
needs the stage and Documents needs ingestion, so the reader is built first. Pass
`PROCESSING_MIGRATIONS` to `migrate.upgrade` and register `job_handlers` with the worker; the
`events` queue must be configured in `LDA_JOBS__QUEUES` (the purge handler is an `ASYNC_JOB` event
handler). Nothing here adds a scanner: the plan's mandatory `scan` stage stays ingestion's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lda.bootstrap.container import PlatformContainer, require_queues
from lda.modules.document_processing.api import (
    ExtractionLimits,
    ExtractStage,
    PurgeExtractions,
    PurgeExtractionsOnDocumentPurged,
    ReadExtraction,
)
from lda.modules.document_processing.config import DocumentProcessingSettings
from lda.modules.document_processing.infrastructure.parsers.registry import FormatParsers
from lda.modules.document_processing.infrastructure.persistence.locations import (
    MIGRATIONS_VERSIONS,
)
from lda.modules.document_processing.infrastructure.persistence.unit_of_work import (
    SqlProcessingUnitOfWorkFactory,
)
from lda.modules.documents.api import DocumentPurged, ReadOriginal, ReadVersionState
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.events.job_handler import EventJobHandler
from lda.platform.events.registry import EVENT_QUEUE
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobHandler

PROCESSING_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class ProcessingContainer:
    stage: ExtractStage
    """Put it in the ingestion plan (after the mandatory scan stage)."""
    purge_extractions: PurgeExtractions
    job_handlers: Mapping[str, JobHandler]
    """Register with the job worker. The `events` queue must exist in `LDA_JOBS__QUEUES`."""


def load_processing_settings(config: ConfigurationLoader) -> DocumentProcessingSettings:
    return config.load("document_processing", DocumentProcessingSettings)


def build_processing(
    platform: PlatformContainer,
    reader: ReadOriginal,
    settings: DocumentProcessingSettings,
) -> ProcessingContainer:
    require_queues(platform, EVENT_QUEUE)
    units_of_work = SqlProcessingUnitOfWorkFactory(platform.units_of_work)
    limits = ExtractionLimits(
        settings.max_bytes,
        settings.max_pages,
        settings.max_characters,
        settings.max_expanded_bytes,
    )
    stage = ExtractStage(
        reader, FormatParsers(), units_of_work, platform.object_store, platform.clock, limits
    )
    purge = PurgeExtractions(units_of_work, platform.object_store, platform.clock)
    handler = PurgeExtractionsOnDocumentPurged(purge)
    platform.handlers.register(DocumentPurged, handler, Delivery.ASYNC_JOB)
    return ProcessingContainer(
        stage=stage,
        purge_extractions=purge,
        job_handlers={
            f"event:{handler.name}": EventJobHandler(
                handler, platform.event_codec, platform.units_of_work
            )
        },
    )


def build_extraction_reader(
    platform: PlatformContainer, versions: ReadVersionState
) -> ReadExtraction:
    """The only way a later module reads extracted text (ADR-038): Documents is asked first, then
    the recorded, hash-checked artifact is read. Stateless, so equivalent to the stage's own
    wiring; built before `build_ingestion` because the chunk stage needs it."""
    return ReadExtraction(
        SqlProcessingUnitOfWorkFactory(platform.units_of_work), platform.object_store, versions
    )
