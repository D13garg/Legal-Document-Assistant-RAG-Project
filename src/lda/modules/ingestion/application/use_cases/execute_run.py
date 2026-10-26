from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from lda.modules.ingestion.application.dto.runs import (
    ExecuteRunCommand,
    ExecutionOutcome,
    ExecutionResult,
)
from lda.modules.ingestion.application.outcomes import outcome_event
from lda.modules.ingestion.application.pipeline import Pipeline
from lda.modules.ingestion.application.ports import (
    IngestionUnitOfWork,
    IngestionUnitOfWorkFactory,
    PipelineStage,
    StageOutcome,
    StagePermanentFailure,
    StageQuarantined,
    StageRequest,
    StageRetryable,
    StageSucceeded,
)
from lda.modules.ingestion.application.starter import RunStarter
from lda.modules.ingestion.domain.errors import InvalidRun, valid_error_code
from lda.modules.ingestion.domain.run import IngestionRun, StageStatus, run_id_for
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import InfrastructureError, LdaError
from lda.shared.kernel.ids import EventId
from lda.shared.ports.clock import Clock, IdGenerator
from lda.shared.ports.events import Delivery

log = logging.getLogger(__name__)

STAGE_EXCEPTION = "stage_exception"
STAGE_OUTCOME_INVALID = "stage_outcome_invalid"
STAGE_CODE_INVALID = "stage_error_code_invalid"
PIPELINE_MISMATCH = "pipeline_mismatch"
LEASE_LOST = "job_lease_lost"


@dataclass(frozen=True)
class _Step:
    stage: str
    attempt: int  # the stage's own attempt counter: the compare-and-set that fences stale executors


@public(reason="Runs only as the ingestion job enqueued for a document version (ADR-010 §2).")
@not_audited(
    reason="Pipeline state by the system; Documents audits the version transition (ADR-010 §15)."
)
class ExecuteRun:
    """Runs the stages of one run in order, one short transaction before and one after each.

    Each stage: (1) lock the run, start the stage, verify the job's fencing token, commit; (2) run
    the stage OUTSIDE any transaction; (3) lock the run again, apply the outcome only if the stage
    is still the one this executor started, verify the fence, publish any terminal event in the
    same transaction, commit. A crash between the two leaves a `running` stage that the next
    delivery runs again (stages are idempotent); a finished stage is never run twice.
    """

    def __init__(
        self,
        units_of_work: IngestionUnitOfWorkFactory,
        pipeline: Pipeline,
        starter: RunStarter,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._units_of_work = units_of_work
        self._pipeline = pipeline
        self._starter = starter
        self._clock = clock
        self._ids = ids

    def execute(self, ctx: SystemContext, command: ExecuteRunCommand) -> ExecutionResult:
        for _ in range(len(self._pipeline.names) + 1):  # every pass finishes a stage or returns
            begun = self._begin(ctx, command)
            if isinstance(begun, ExecutionResult):
                return begun
            outcome = self._run_stage(ctx, command, begun)
            result = self._record(ctx, command, begun, outcome)
            if result is not None:
                return result
        raise InfrastructureError("ingestion_run_not_progressing")

    # ---- transaction 1: start the next stage -----------------------------------------------

    def _begin(self, ctx: SystemContext, command: ExecuteRunCommand) -> _Step | ExecutionResult:
        with self._units_of_work.begin(ctx) as uow:
            now = self._clock.now()
            run = uow.runs.get_for_version(command.version, command.generation, for_update=True)
            if run is None:
                run = self._starter.start(
                    uow, ctx.tenant_id, command.document, command.version, command.generation, now
                ) or uow.runs.get_for_version(command.version, command.generation, for_update=True)
            if run is None or run.document != command.document or not run.is_active:
                return ExecutionResult(ExecutionOutcome.INACTIVE)  # nothing is committed
            stage = run.next_stage()
            if stage is None:  # every stage succeeded but the run was not closed: close it now
                run.complete(now)
                self._close(uow, run, command, now)
                return ExecutionResult(ExecutionOutcome.FINISHED)
            if self._pipeline.get(stage.name) is None:  # the plan changed under a live run
                run.begin_stage(stage.name, now)
                run.fail_stage(stage.name, PIPELINE_MISMATCH, now)
                self._close(uow, run, command, now)
                return ExecutionResult(ExecutionOutcome.FINISHED, PIPELINE_MISMATCH)
            run.begin_stage(stage.name, now)
            uow.runs.save(run)
            uow.fence.verify(command.job_id, command.fencing_token)
            uow.commit()
            return _Step(stage.name, run.stage(stage.name).attempts)

    # ---- the stage itself: outside any transaction -----------------------------------------

    def _run_stage(
        self, ctx: SystemContext, command: ExecuteRunCommand, step: _Step
    ) -> StageOutcome:
        stage: PipelineStage | None = self._pipeline.get(step.stage)
        if stage is None:  # cannot happen: _begin checked it
            return StageRetryable(PIPELINE_MISMATCH)
        request = StageRequest(
            ctx.tenant_id,
            command.document,
            command.version,
            run_id_for(ctx.tenant_id, command.version, command.generation),
            step.stage,
            step.attempt,
            command.job_id,
            command.fencing_token,
        )
        try:
            outcome = stage.run(request)
        except LdaError as exc:
            if exc.code == LEASE_LOST:
                raise  # a stage's own fence failed: this executor must stop, not retry
            return StageRetryable(exc.code)
        except Exception:
            return StageRetryable(STAGE_EXCEPTION)
        if not isinstance(
            outcome, (StageSucceeded, StageRetryable, StagePermanentFailure, StageQuarantined)
        ):
            return StageRetryable(STAGE_OUTCOME_INVALID)
        return outcome

    # ---- transaction 2: apply the outcome --------------------------------------------------

    def _record(
        self, ctx: SystemContext, command: ExecuteRunCommand, step: _Step, outcome: StageOutcome
    ) -> ExecutionResult | None:
        with self._units_of_work.begin(ctx) as uow:
            now = self._clock.now()
            run = uow.runs.get_for_version(command.version, command.generation, for_update=True)
            if run is None or not run.is_active:
                return ExecutionResult(ExecutionOutcome.INACTIVE)
            stage = run.stage(step.stage)
            if stage.status is not StageStatus.RUNNING or stage.attempts != step.attempt:
                return ExecutionResult(ExecutionOutcome.INACTIVE)  # a newer executor owns it
            started = stage.started_at
            result: ExecutionResult | None
            code: str | None = None
            if isinstance(outcome, StageSucceeded):
                run.succeed_stage(step.stage, now)
                if run.next_stage() is None:
                    run.complete(now)
                    self._close(uow, run, command, now)
                    result = ExecutionResult(ExecutionOutcome.FINISHED)
                else:
                    self._save(uow, run, command)
                    result = None
            elif isinstance(outcome, StageRetryable):
                code = self._safe(outcome.error_code)
                run.retry_stage(step.stage, code, now)
                if command.attempt >= command.max_attempts:
                    run.dead_letter(code, now)
                    result = ExecutionResult(ExecutionOutcome.DEAD_LETTERED, code)
                else:
                    result = ExecutionResult(ExecutionOutcome.RETRY, code)
                self._save(uow, run, command)
            elif isinstance(outcome, StagePermanentFailure):
                code = self._safe(outcome.error_code)
                run.fail_stage(step.stage, code, now)
                self._close(uow, run, command, now)
                result = ExecutionResult(ExecutionOutcome.FINISHED, code)
            else:
                code = self._safe(outcome.error_code)
                run.quarantine_stage(step.stage, code, now)
                self._close(uow, run, command, now)
                result = ExecutionResult(ExecutionOutcome.FINISHED, code)
            fields = {
                "stage": step.stage,
                "outcome": type(outcome).__name__,
                "run_status": run.status.value,
                "duration_ms": 0
                if started is None
                else int((now - started).total_seconds() * 1000),
            }
            if code is not None:
                fields["error_code"] = code
            log.info("ingestion_stage_finished", extra={"fields": fields})
            return result

    # ---- shared ----------------------------------------------------------------------------

    def _save(
        self, uow: IngestionUnitOfWork, run: IngestionRun, command: ExecuteRunCommand
    ) -> None:
        uow.runs.save(run)
        uow.fence.verify(command.job_id, command.fencing_token)
        uow.commit()

    def _close(
        self, uow: IngestionUnitOfWork, run: IngestionRun, command: ExecuteRunCommand, now: datetime
    ) -> None:
        """Persist a run that reached an outcome and publish it in the SAME transaction."""
        uow.events.publish(
            outcome_event(run, EventId(self._ids.new_uuid()), now), Delivery.ASYNC_JOB
        )
        self._save(uow, run, command)

    @staticmethod
    def _safe(code: str) -> str:
        try:
            return valid_error_code(code)
        except InvalidRun:
            return STAGE_CODE_INVALID
