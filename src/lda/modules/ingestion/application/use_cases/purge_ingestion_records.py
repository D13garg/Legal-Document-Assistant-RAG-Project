from __future__ import annotations

from lda.modules.ingestion.application.dto.runs import DocumentReferenceCommand
from lda.modules.ingestion.application.ports import IngestionUnitOfWorkFactory
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public


@public(reason="Called by Documents' handler of its own DocumentPurged event (ADR-023 amendment).")
@not_audited(
    reason="Removes ingestion-owned identifiers and counters; Documents holds the receipt."
)
class PurgeIngestionRecords:
    """Deletes ONLY ingestion-owned state of a document (runs and stage runs). It writes no
    receipt and claims nothing about any other module's data (ADR-023 amendment)."""

    def __init__(self, units_of_work: IngestionUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def execute(self, ctx: SystemContext, command: DocumentReferenceCommand) -> int:
        with self._units_of_work.begin(ctx) as uow:
            removed = uow.runs.remove_for_document(command.document)
            uow.commit()
            return removed
