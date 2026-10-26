from __future__ import annotations

from lda.modules.ingestion.application.dto.runs import RunReference
from lda.modules.ingestion.application.ports import IngestionUnitOfWorkFactory
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.ports.clock import Clock


@public(reason="Called by Documents' outcome handlers once they applied or refused an outcome.")
@not_audited(reason="Delivery bookkeeping on ingestion-owned state (ADR-010 §15).")
class AcknowledgeOutcome:
    """Idempotent: an unknown run, a run without an outcome or a repeated call changes nothing."""

    def __init__(self, units_of_work: IngestionUnitOfWorkFactory, clock: Clock) -> None:
        self._units_of_work = units_of_work
        self._clock = clock

    def execute(self, ctx: SystemContext, command: RunReference) -> bool:
        with self._units_of_work.begin(ctx) as uow:
            run = uow.runs.get(command.run_id, for_update=True)
            if run is None or not run.has_outcome:
                return False
            if not run.acknowledge(self._clock.now()):
                return False
            uow.runs.save(run)
            uow.commit()
            return True
