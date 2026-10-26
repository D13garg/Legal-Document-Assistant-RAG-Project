from __future__ import annotations

import math
from datetime import timedelta

from lda.modules.ingestion.application.dto.runs import WatchdogCommand, WatchdogResult
from lda.modules.ingestion.application.outcomes import outcome_event
from lda.modules.ingestion.application.ports import IngestionUnitOfWorkFactory
from lda.modules.ingestion.domain.run import RunStatus
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.ids import EventId
from lda.shared.ports.clock import Clock, IdGenerator
from lda.shared.ports.events import Delivery

STALLED_CODE = "watchdog_stalled"


@public(
    reason="Runs only as the delayed per-run watchdog job enqueued with every run (ADR-010 §11)."
)
@not_audited(
    reason="Recovery bookkeeping on ingestion-owned state; no business mutation (ADR-010 §15)."
)
class RunWatchdog:
    """The recovery path, tenant-bound and idempotent (ADR-010 §11).

    - A `running` run with no progress for `delay` is dead-lettered (`watchdog_stalled`): its job
      died without reaching a handler. This is NOT a replay; a person replays explicitly.
    - A run with an outcome that Documents has not acknowledged for `delay` is re-published with a
      new event id, at most `max_redeliveries` times.
    - Everything else is finished (`done`) or asks to be checked again without using an attempt.
    """

    def __init__(
        self,
        units_of_work: IngestionUnitOfWorkFactory,
        clock: Clock,
        ids: IdGenerator,
        delay: timedelta,
        max_redeliveries: int,
    ) -> None:
        self._units_of_work = units_of_work
        self._clock = clock
        self._ids = ids
        self._delay = delay
        self._max_redeliveries = max_redeliveries

    def execute(self, ctx: SystemContext, command: WatchdogCommand) -> WatchdogResult:
        with self._units_of_work.begin(ctx) as uow:
            run = uow.runs.get(command.run_id, for_update=True)
            if run is None:
                return WatchdogResult(done=True)
            now = self._clock.now()
            idle = now - run.progress_at
            if run.status in (RunStatus.DEAD_LETTERED, RunStatus.CANCELLED):
                return WatchdogResult(done=True)
            if run.has_outcome and run.acknowledged_at is not None:
                return WatchdogResult(done=True)
            if idle < self._delay:
                return WatchdogResult(done=False, next_check_seconds=self._wait(self._delay - idle))
            if run.status is RunStatus.RUNNING:
                run.dead_letter(STALLED_CODE, now)
                uow.runs.save(run)
                uow.fence.verify(command.job_id, command.fencing_token)
                uow.commit()
                return WatchdogResult(done=True)
            if run.redeliveries >= self._max_redeliveries:
                return WatchdogResult(done=True)  # visible in the metrics; an operator decides
            uow.events.publish(
                outcome_event(run, EventId(self._ids.new_uuid()), now), Delivery.ASYNC_JOB
            )
            run.record_redelivery(now)
            uow.runs.save(run)
            uow.fence.verify(command.job_id, command.fencing_token)
            uow.commit()
            return WatchdogResult(done=False, next_check_seconds=self._wait(self._delay))

    @staticmethod
    def _wait(remaining: timedelta) -> int:
        return math.ceil(remaining.total_seconds())  # remaining > 0, so at least one second
