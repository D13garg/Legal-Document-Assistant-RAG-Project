from __future__ import annotations

from lda.modules.ingestion.application.dto.runs import RunView, StageView, VersionQuery
from lda.modules.ingestion.application.ports import IngestionUnitOfWorkFactory
from lda.modules.ingestion.domain.run import IngestionRun
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import not_audited, requires


def view_of(run: IngestionRun) -> RunView:
    return RunView(
        run.id,
        run.document,
        run.version,
        run.generation,
        run.status.value,
        run.error_code,
        run.acknowledged_at is not None,
        run.redeliveries,
        tuple(
            StageView(s.name, s.status.value, s.attempts, s.duration_ms, s.error_code)
            for s in run.stages
        ),
    )


@requires("ingestion.read_status")
@not_audited(reason="Read-only operational query; identifiers and statuses only (ADR-019 D2).")
class GetIngestionStatus:
    """The latest generation of a version's run, or None. Never returns content."""

    def __init__(self, units_of_work: IngestionUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def execute(self, ctx: ExecutionContext, query: VersionQuery) -> RunView | None:
        with self._units_of_work.begin(ctx) as uow:
            run = uow.runs.latest_for_version(query.version)
            return None if run is None else view_of(run)
