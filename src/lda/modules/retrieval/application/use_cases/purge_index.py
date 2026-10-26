"""`PurgeRetrievalIndex`: deletes this module's derived data of one purged document (ADR-023)."""

from __future__ import annotations

from lda.modules.retrieval.application.dto import PurgeCommand, PurgeView
from lda.modules.retrieval.application.ports import RetrievalUnitOfWorkFactory
from lda.modules.retrieval.domain.record import PurgeReceipt
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.ports.clock import Clock


@public(reason="Runs only as the handler of Documents' DocumentPurged event (ADR-023, ADR-008).")
@not_audited(
    reason="Removes derived vectors and identifiers; the receipt is the evidence and Documents "
    "audits the purge itself (ADR-019 D2)."
)
class PurgeRetrievalIndex:
    """One transaction under the document's lifecycle lock: lists the rows, deletes them and writes
    an insert-only receipt. A stage that recorded before this point is seen and removed; a stage
    that comes after the receipt refuses. Everything is Postgres rows, so there is no second store
    to verify and no intermediate state. The receipt claims only `PURGE_SCOPE`. Idempotent: a
    document with a receipt is left alone (a retry, a duplicate event)."""

    def __init__(self, units_of_work: RetrievalUnitOfWorkFactory, clock: Clock) -> None:
        self._units_of_work = units_of_work
        self._clock = clock

    def execute(self, ctx: SystemContext, command: PurgeCommand) -> PurgeView:
        with self._units_of_work.begin(ctx) as uow:
            uow.index.lock_document(command.document_id)
            if uow.receipts.get(command.document_id) is not None:
                return PurgeView(command.document_id, purged=False, versions_purged=0)
            versions = tuple(sorted(uow.index.list_for(command.document_id), key=str))
            uow.index.remove_all_for(command.document_id)
            uow.receipts.add(
                PurgeReceipt(ctx.tenant_id, command.document_id, self._clock.now(), versions)
            )
            uow.commit()
            return PurgeView(command.document_id, purged=True, versions_purged=len(versions))
