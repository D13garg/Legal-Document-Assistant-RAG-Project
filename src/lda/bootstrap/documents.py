"""Documents wiring (ADR-005, ADR-015, ADR-023).

Names here say `docs`, not `document`: a Step 1 architecture test keeps the word out of every class,
function and argument name under `bootstrap/` and `platform/` so that wiring stays domain-agnostic.

Order matters: `register_doc_resources(registry)` BEFORE `build_access_control` (it seals the
registry), then `build_ingestion(...)`, then `build_docs(platform, access_control, ingestion,
settings)`. Documents imports only ingestion's public API (ADR-010); the revoker that makes
tombstone and revocation one transaction is Access Control's adapter, handed in here as a factory
(the same pattern as the audit recorder). `build_docs` also registers Documents' `ASYNC_JOB` event
handlers and adds their `event:<name>` jobs to `job_handlers`: the `events` queue must be
configured in `LDA_JOBS__QUEUES` next to `documents` and `ingestion`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lda.bootstrap.access_control import AccessControlContainer
from lda.bootstrap.container import PlatformContainer, require_queues
from lda.bootstrap.ingestion import IngestionContainer
from lda.modules.access_control.api import ActionId, ResourceKindDefinition, ResourceRegistry
from lda.modules.documents.api import (
    DOCUMENT_ACTIONS,
    DOCUMENT_KIND,
    DOCUMENTS_QUEUE,
    PURGE_JOB,
    UPLOAD_INTENT_JOB,
    AddDocumentVersion,
    CancelPipelineOnDocumentDeleted,
    ClearPipelineRecordsOnPurge,
    DeleteDocument,
    DocumentDeleted,
    DocumentPurged,
    DocumentVersionAdded,
    GetDocument,
    PurgeDocument,
    PurgeJobHandler,
    ReadOriginal,
    ReadVersionState,
    ReapAbandonedUpload,
    RecordFailedOnFailed,
    RecordQuarantinedOnQuarantined,
    RecordReadyOnCompleted,
    RecordVersionOutcome,
    StartPipelineOnVersionAdded,
    UploadDocument,
    UploadIntentJobHandler,
    UploadPolicy,
    UploadStaging,
    VerifyDocumentPurge,
)
from lda.modules.documents.config import DocumentsSettings
from lda.modules.documents.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.documents.infrastructure.persistence.unit_of_work import (
    SqlDocumentsUnitOfWorkFactory,
)
from lda.modules.ingestion.api import (
    IngestionCompleted,
    IngestionFailed,
    IngestionQuarantined,
)
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.events.job_handler import EventJobHandler
from lda.platform.events.registry import EVENT_QUEUE
from lda.shared.kernel.events import DomainEvent
from lda.shared.ports.event_handlers import EventHandler
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobHandler

DOCUMENTS_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class DocsContainer:
    upload_document: UploadDocument
    add_document_version: AddDocumentVersion
    delete_document: DeleteDocument
    get_document: GetDocument
    verify_document_purge: VerifyDocumentPurge
    purge_document: PurgeDocument
    reap_abandoned_upload: ReapAbandonedUpload
    record_version_outcome: RecordVersionOutcome
    job_handlers: Mapping[str, JobHandler]
    """Register with the job worker. The `documents` queue must exist in `LDA_JOBS__QUEUES`."""


def load_docs_settings(config: ConfigurationLoader) -> DocumentsSettings:
    return config.load("documents", DocumentsSettings)


def register_doc_resources(registry: ResourceRegistry) -> None:
    """The owning module contributes its resource kind and actions (ADR-014 D2)."""
    registry.register(
        ResourceKindDefinition(DOCUMENT_KIND, frozenset(ActionId(a) for a in DOCUMENT_ACTIONS))
    )


def build_original_reader(
    platform: PlatformContainer, access_control: AccessControlContainer
) -> ReadOriginal:
    """The pipeline's only content read path (ADR-037). Built BEFORE `build_ingestion`, because a
    stage needs it and `build_docs` needs the ingestion container; the unit-of-work factory it uses
    is stateless, so it is equivalent to the one `build_docs` creates."""
    units_of_work = SqlDocumentsUnitOfWorkFactory(
        platform.units_of_work, access_control.revoker_factory
    )
    return ReadOriginal(units_of_work, platform.object_store)


def build_version_state_reader(
    platform: PlatformContainer, access_control: AccessControlContainer
) -> ReadVersionState:
    """Documents' lifecycle verdict for derived-data modules (ADR-038). Like the original reader
    it is built before `build_ingestion`; the unit-of-work factory is stateless."""
    units_of_work = SqlDocumentsUnitOfWorkFactory(
        platform.units_of_work, access_control.revoker_factory
    )
    return ReadVersionState(units_of_work)


def build_docs(
    platform: PlatformContainer,
    access_control: AccessControlContainer,
    ingestion: IngestionContainer,
    settings: DocumentsSettings,
) -> DocsContainer:
    require_queues(platform, DOCUMENTS_QUEUE, EVENT_QUEUE)
    for owned in (DocumentVersionAdded, DocumentDeleted, DocumentPurged):
        platform.event_codec.register(owned)  # so later handlers can decode them
    units_of_work = SqlDocumentsUnitOfWorkFactory(
        platform.units_of_work, access_control.revoker_factory
    )
    policy = UploadPolicy(
        settings.max_upload_bytes,
        frozenset(settings.allowed_media_types),
        settings.upload_intent_grace_seconds,
    )
    store, clock, ids = platform.object_store, platform.clock, platform.ids
    staging = UploadStaging(units_of_work, store, clock, ids, policy)
    purge = PurgeDocument(units_of_work, store, clock, ids)
    reap = ReapAbandonedUpload(units_of_work, store, clock, policy)
    record = RecordVersionOutcome(units_of_work, clock)
    acknowledge, purge_records = ingestion.acknowledge_outcome, ingestion.purge_ingestion_records
    wiring: tuple[tuple[type[DomainEvent], EventHandler], ...] = (
        (DocumentVersionAdded, StartPipelineOnVersionAdded()),
        (IngestionCompleted, RecordReadyOnCompleted(record, acknowledge, purge_records)),
        (IngestionFailed, RecordFailedOnFailed(record, acknowledge, purge_records)),
        (
            IngestionQuarantined,
            RecordQuarantinedOnQuarantined(record, acknowledge, purge_records),
        ),
        (DocumentDeleted, CancelPipelineOnDocumentDeleted(ingestion.cancel_ingestion)),
        (DocumentPurged, ClearPipelineRecordsOnPurge(ingestion.purge_ingestion_records)),
    )
    event_jobs: dict[str, JobHandler] = {}
    for event_type, handler in wiring:
        platform.handlers.register(event_type, handler, Delivery.ASYNC_JOB)
        event_jobs[f"event:{handler.name}"] = EventJobHandler(
            handler, platform.event_codec, platform.units_of_work
        )
    return DocsContainer(
        upload_document=UploadDocument(units_of_work, staging, clock, ids),
        add_document_version=AddDocumentVersion(units_of_work, staging, clock, ids),
        delete_document=DeleteDocument(units_of_work, clock, ids),
        get_document=GetDocument(units_of_work),
        verify_document_purge=VerifyDocumentPurge(units_of_work, store),
        purge_document=purge,
        reap_abandoned_upload=reap,
        record_version_outcome=record,
        job_handlers={
            PURGE_JOB: PurgeJobHandler(purge),
            UPLOAD_INTENT_JOB: UploadIntentJobHandler(reap),
            **event_jobs,
        },
    )
