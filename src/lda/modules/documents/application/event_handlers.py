"""Documents' reactions to events (ADR-010 §2, §7, §10; ADR-023 amendment). All are `ASYNC_JOB`
handlers: each runs in its own job, so a failure retries with backoff and finally dead-letters.

The platform hands a handler the event's own unit of work; these handlers deliberately ignore it
and call the use cases, which open their own (tenant-bound) transactions. Every handler is
idempotent, so redelivery of an event is harmless. Names avoid the word the Step 5 boundary test
reserves: this module orchestrates nothing, it only reacts.
"""

from __future__ import annotations

from lda.modules.documents.application.dto.documents import (
    OutcomeResult,
    RecordOutcomeCommand,
)
from lda.modules.documents.application.use_cases.record_version_outcome import (
    RecordVersionOutcome,
)
from lda.modules.documents.domain.document import VersionStatus
from lda.modules.documents.domain.events import (
    DocumentDeleted,
    DocumentPurged,
    DocumentVersionAdded,
)
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.ingestion.api import (
    AcknowledgeOutcome,
    CancelIngestion,
    DocumentRef,
    DocumentReferenceCommand,
    IngestionCompleted,
    IngestionFailed,
    IngestionQuarantined,
    PurgeIngestionRecords,
    RunReference,
    ingestion_job_spec,
)
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.ports.unit_of_work import UnitOfWork

UNEXPECTED_EVENT = "event_type_unexpected"


class StartPipelineOnVersionAdded:
    """A new version enters the pipeline: one job per version, enqueued in the handler's own
    transaction, unique on (version, generation 1) so redelivery cannot start it twice."""

    name = "documents.start_pipeline"

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        if not isinstance(event, DocumentVersionAdded):
            raise ValidationError(UNEXPECTED_EVENT)
        uow.jobs.enqueue(ingestion_job_spec(event.document_id.value, event.version_id.value))


class _OutcomeHandler:
    """Applies one pipeline outcome to the version, then acknowledges it to ingestion. If the
    version no longer exists (purged), ingestion's records for the document are removed too."""

    def __init__(
        self,
        record: RecordVersionOutcome,
        acknowledge: AcknowledgeOutcome,
        purge: PurgeIngestionRecords,
    ) -> None:
        self._record = record
        self._acknowledge = acknowledge
        self._purge = purge

    def _apply(
        self,
        tenant_event: DomainEvent,
        document: DocumentRef,
        version_id: DocumentVersionId,
        run: RunReference,
        outcome: VersionStatus,
    ) -> None:
        ctx = SystemContext(tenant_event.tenant_id, "documents:record_version_outcome")
        result = self._record.execute(
            ctx, RecordOutcomeCommand(DocumentId(document.value), version_id, outcome)
        )
        self._acknowledge.execute(ctx, run)
        if result.result is OutcomeResult.MISSING:
            self._purge.execute(ctx, DocumentReferenceCommand(document))


class RecordReadyOnCompleted(_OutcomeHandler):
    name = "documents.record_ready"

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        if not isinstance(event, IngestionCompleted):
            raise ValidationError(UNEXPECTED_EVENT)
        self._apply(
            event,
            event.document,
            DocumentVersionId(event.version.value),
            RunReference(event.run_id),
            VersionStatus.READY,
        )


class RecordFailedOnFailed(_OutcomeHandler):
    name = "documents.record_failed"

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        if not isinstance(event, IngestionFailed):
            raise ValidationError(UNEXPECTED_EVENT)
        self._apply(
            event,
            event.document,
            DocumentVersionId(event.version.value),
            RunReference(event.run_id),
            VersionStatus.FAILED,
        )


class RecordQuarantinedOnQuarantined(_OutcomeHandler):
    name = "documents.record_quarantined"

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        if not isinstance(event, IngestionQuarantined):
            raise ValidationError(UNEXPECTED_EVENT)
        self._apply(
            event,
            event.document,
            DocumentVersionId(event.version.value),
            RunReference(event.run_id),
            VersionStatus.QUARANTINED,
        )


class CancelPipelineOnDocumentDeleted:
    name = "documents.cancel_pipeline"

    def __init__(self, cancel: CancelIngestion) -> None:
        self._cancel = cancel

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        if not isinstance(event, DocumentDeleted):
            raise ValidationError(UNEXPECTED_EVENT)
        ctx = SystemContext(event.tenant_id, "documents:cancel_pipeline")
        self._cancel.execute(ctx, DocumentReferenceCommand(DocumentRef(event.document_id.value)))


class ClearPipelineRecordsOnPurge:
    name = "documents.clear_pipeline_records"

    def __init__(self, purge: PurgeIngestionRecords) -> None:
        self._purge = purge

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        if not isinstance(event, DocumentPurged):
            raise ValidationError(UNEXPECTED_EVENT)
        ctx = SystemContext(event.tenant_id, "documents:clear_pipeline_records")
        self._purge.execute(ctx, DocumentReferenceCommand(DocumentRef(event.document_id.value)))
