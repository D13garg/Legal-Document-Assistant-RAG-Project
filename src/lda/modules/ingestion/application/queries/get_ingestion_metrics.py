from __future__ import annotations

from lda.modules.ingestion.application.dto.runs import IngestionMetrics
from lda.modules.ingestion.application.ports import IngestionUnitOfWorkFactory
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import not_audited, requires


@requires("ingestion.read_metrics")
@not_audited(reason="Read-only operational query; counters and durations only (ADR-019 D2).")
class GetIngestionMetrics:
    """Run and stage counters of ONE tenant, computed in SQL (ADR-010 §14). Queue depth, oldest
    age and dead jobs are the platform's `JobMaintenance.metrics`."""

    def __init__(self, units_of_work: IngestionUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def execute(self, ctx: ExecutionContext) -> IngestionMetrics:
        with self._units_of_work.begin(ctx) as uow:
            return uow.runs.metrics()
