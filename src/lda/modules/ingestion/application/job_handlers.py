"""Adapters from the job queue to the use cases (ADR-010). The tenant comes from `ctx` (the job
row); the payload carries opaque ids only."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from lda.modules.ingestion.application.dto.runs import (
    ExecuteRunCommand,
    ExecutionOutcome,
    WatchdogCommand,
)
from lda.modules.ingestion.application.errors import RetryRequested
from lda.modules.ingestion.application.use_cases.execute_run import ExecuteRun
from lda.modules.ingestion.application.use_cases.run_watchdog import RunWatchdog
from lda.modules.ingestion.domain.ids import DocumentRef, IngestionRunId, VersionRef
from lda.shared.kernel.context import SystemContext
from lda.shared.ports.jobs import ClaimedJob, Failed, JobOutcome, RetryAfter, Succeeded

PAYLOAD_INVALID = "job_payload_invalid"


def _uuid(job: ClaimedJob, field: str) -> UUID | None:
    try:
        return UUID(str(job.payload[field]))
    except (KeyError, ValueError):
        return None


def _generation(job: ClaimedJob) -> int | None:
    value = job.payload.get("generation")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


class RunJobHandler:
    def __init__(self, execute_run: ExecuteRun) -> None:
        self._execute_run = execute_run

    def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
        document, version = _uuid(job, "document_id"), _uuid(job, "version_id")
        generation = _generation(job)
        if document is None or version is None or generation is None:
            return Failed(error_code=PAYLOAD_INVALID)
        result = self._execute_run.execute(
            ctx,
            ExecuteRunCommand(
                DocumentRef(document),
                VersionRef(version),
                generation,
                job.id,
                job.fencing_token,
                job.attempt,
                job.max_attempts,
            ),
        )
        if result.outcome is ExecutionOutcome.RETRY:
            raise RetryRequested(result.error_code or "ingestion_retry")  # the worker backs off
        if result.outcome is ExecutionOutcome.DEAD_LETTERED:
            return Failed(error_code=result.error_code or "ingestion_dead_lettered")
        return Succeeded()


class WatchdogJobHandler:
    def __init__(self, watchdog: RunWatchdog) -> None:
        self._watchdog = watchdog

    def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
        run_id = _uuid(job, "run_id")
        if run_id is None:
            return Failed(error_code=PAYLOAD_INVALID)
        result = self._watchdog.execute(
            ctx, WatchdogCommand(IngestionRunId(run_id), job.id, job.fencing_token)
        )
        if result.done:
            return Succeeded()
        return RetryAfter(
            delay=timedelta(seconds=result.next_check_seconds),
            error_code="ingestion_watchdog_pending",
            consumes_attempt=False,
        )
