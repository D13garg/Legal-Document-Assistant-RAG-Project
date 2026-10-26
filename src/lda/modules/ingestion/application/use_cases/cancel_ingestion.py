from __future__ import annotations

from lda.modules.ingestion.application.dto.runs import DocumentReferenceCommand
from lda.modules.ingestion.application.ports import IngestionUnitOfWorkFactory
from lda.modules.ingestion.domain.run import RunStatus
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.ports.clock import Clock


@public(reason="Called by Documents' handler of its own DocumentDeleted event (ADR-023 amendment).")
@not_audited(
    reason="Cleanup of ingestion-owned state; the deletion itself is audited by Documents."
)
class CancelIngestion:
    """Stops every unfinished run of a deleted document. Idempotent; finished outcomes stay."""

    def __init__(self, units_of_work: IngestionUnitOfWorkFactory, clock: Clock) -> None:
        self._units_of_work = units_of_work
        self._clock = clock

    def execute(self, ctx: SystemContext, command: DocumentReferenceCommand) -> int:
        with self._units_of_work.begin(ctx) as uow:
            now = self._clock.now()
            cancelled = 0
            for run in uow.runs.list_for_document(command.document, for_update=True):
                if run.status in (RunStatus.RUNNING, RunStatus.DEAD_LETTERED):
                    run.cancel(now)
                    uow.runs.save(run)
                    cancelled += 1
            uow.commit()
            return cancelled
