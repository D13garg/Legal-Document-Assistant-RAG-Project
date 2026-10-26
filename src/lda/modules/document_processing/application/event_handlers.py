"""document_processing's reaction to Documents' events (ADR-023, ADR-037). An `ASYNC_JOB` handler:
it runs in its own job, so a failure retries with backoff and finally dead-letters. The platform
hands it the event's own unit of work; the handler ignores it and calls the use case, which opens
its own tenant-bound transactions."""

from __future__ import annotations

from lda.modules.document_processing.application.dto import PurgeCommand
from lda.modules.document_processing.application.use_cases.purge_extractions import PurgeExtractions
from lda.modules.document_processing.domain.ids import DocumentId
from lda.modules.documents.api import DocumentPurged
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.ports.unit_of_work import UnitOfWork

UNEXPECTED_EVENT = "event_type_unexpected"


class PurgeExtractionsOnDocumentPurged:
    name = "document_processing.purge_extractions"

    def __init__(self, purge: PurgeExtractions) -> None:
        self._purge = purge

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        if not isinstance(event, DocumentPurged):
            raise ValidationError(UNEXPECTED_EVENT)
        ctx = SystemContext(event.tenant_id, "document_processing:purge_extractions")
        self._purge.execute(ctx, PurgeCommand(DocumentId(event.document_id.value)))
