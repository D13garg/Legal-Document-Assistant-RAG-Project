from __future__ import annotations

from datetime import datetime, timedelta

from lda.modules.ingestion.application.jobs import watchdog_job_spec
from lda.modules.ingestion.application.pipeline import Pipeline
from lda.modules.ingestion.application.ports import IngestionUnitOfWork
from lda.modules.ingestion.domain.ids import DocumentRef, VersionRef
from lda.modules.ingestion.domain.run import IngestionRun
from lda.shared.kernel.ids import TenantId


class RunStarter:
    """Creates a run with the frozen plan and its watchdog in the caller's transaction."""

    def __init__(self, pipeline: Pipeline, watchdog_delay: timedelta) -> None:
        self._pipeline = pipeline
        self._watchdog_delay = watchdog_delay

    def start(
        self,
        uow: IngestionUnitOfWork,
        tenant_id: TenantId,
        document: DocumentRef,
        version: VersionRef,
        generation: int,
        now: datetime,
    ) -> IngestionRun | None:
        """The new run, or None when it already exists (a concurrent executor created it first)."""
        run = IngestionRun.start(
            tenant_id, document, version, generation, self._pipeline.names, now
        )
        if not uow.runs.add(run):
            return None
        uow.jobs.enqueue(watchdog_job_spec(run.id, now + self._watchdog_delay))
        return run
