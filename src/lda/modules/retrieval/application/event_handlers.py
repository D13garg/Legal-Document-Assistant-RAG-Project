"""retrieval's reaction to Documents' events (ADR-023, ADR-008). An `ASYNC_JOB` handler: it runs in
its own job, so a failure retries with backoff and finally dead-letters.

Only `DocumentPurged` is handled. `DocumentDeleted` needs no handler by decision: nothing is served
for a tombstoned document because every search asks Documents for each candidate document's live
state first (`ReadVersionState` answers `not found` from the moment of deletion), and a stage that
runs after deletion is refused at `ReadChunks`, which asks Documents too. A tombstone copied here
could only lag behind Documents' own. The vectors of a deleted document stay until the purge
removes them (ADR-023's retention window); they are unreachable meanwhile.
"""

from __future__ import annotations

from lda.modules.documents.api import DocumentPurged
from lda.modules.retrieval.application.dto import PurgeCommand
from lda.modules.retrieval.application.use_cases.purge_index import PurgeRetrievalIndex
from lda.modules.retrieval.domain.ids import DocumentId
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.ports.unit_of_work import UnitOfWork

UNEXPECTED_EVENT = "event_type_unexpected"


class PurgeRetrievalIndexOnDocumentPurged:
    name = "retrieval.purge_index"

    def __init__(self, purge: PurgeRetrievalIndex) -> None:
        self._purge = purge

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        if not isinstance(event, DocumentPurged):
            raise ValidationError(UNEXPECTED_EVENT)
        ctx = SystemContext(event.tenant_id, "retrieval:purge_index")
        self._purge.execute(ctx, PurgeCommand(DocumentId(event.document_id.value)))
