"""chunking's reaction to Documents' events (ADR-023, ADR-038). An `ASYNC_JOB` handler: it runs in
its own job, so a failure retries with backoff and finally dead-letters. The platform hands it the
event's own unit of work; the handler ignores it and calls the use case, which opens its own
tenant-bound transactions.

Only `DocumentPurged` is handled. `DocumentDeleted` needs no handler (ADR-038 §8): this module has
no read path, the only consumer of its data is its own stage, and that stage reads through
`document_processing.api.ReadExtraction`, which asks Documents first, so nothing is derived for
(or served from) a tombstoned document from the moment it is deleted. A copy of the tombstone kept
here could only lag behind Documents' own, and would be a second source of truth.
"""

from __future__ import annotations

from lda.modules.chunking.application.dto import PurgeCommand
from lda.modules.chunking.application.use_cases.purge_chunks import PurgeChunks
from lda.modules.chunking.domain.ids import DocumentId
from lda.modules.documents.api import DocumentPurged
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.ports.unit_of_work import UnitOfWork

UNEXPECTED_EVENT = "event_type_unexpected"


class PurgeChunksOnDocumentPurged:
    name = "chunking.purge_chunks"

    def __init__(self, purge: PurgeChunks) -> None:
        self._purge = purge

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        if not isinstance(event, DocumentPurged):
            raise ValidationError(UNEXPECTED_EVENT)
        ctx = SystemContext(event.tenant_id, "chunking:purge_chunks")
        self._purge.execute(ctx, PurgeCommand(DocumentId(event.document_id.value)))
