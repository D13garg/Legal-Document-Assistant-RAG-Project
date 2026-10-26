from __future__ import annotations

from lda.modules.ingestion.application.attribution import attribution_of
from lda.modules.ingestion.application.dto.runs import ReplayCommand, ReplayView
from lda.modules.ingestion.application.jobs import ingestion_job_spec
from lda.modules.ingestion.application.ports import IngestionUnitOfWorkFactory
from lda.modules.ingestion.application.starter import RunStarter
from lda.modules.ingestion.domain.run import RunStatus
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import ConflictError, NotFoundError
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock


@requires("ingestion.replay_ingestion")
@audited("ingestion.ingestion_replayed")
class ReplayIngestion:
    """Explicit recovery of a dead-lettered run (ADR-010 §9).

    Allowed only when the LATEST generation of the version is `dead_lettered`. It creates the next
    generation (run row, job with the next unique key, watchdog) in one transaction. It never
    resets a version and never touches a `failed`, `quarantined`, `completed` or `cancelled` run.
    Who may call it is decided by the API layer (Step 16); this only declares the action.
    """

    def __init__(
        self, units_of_work: IngestionUnitOfWorkFactory, starter: RunStarter, clock: Clock
    ) -> None:
        self._units_of_work = units_of_work
        self._starter = starter
        self._clock = clock

    def execute(self, ctx: ExecutionContext, command: ReplayCommand) -> ReplayView:
        with self._units_of_work.begin(ctx) as uow:
            latest = uow.runs.latest_for_version(command.version, for_update=True)
            if latest is None:
                raise NotFoundError("ingestion_run_not_found")
            if latest.status is not RunStatus.DEAD_LETTERED:
                raise ConflictError("ingestion_replay_not_allowed")
            now = self._clock.now()
            run = self._starter.start(
                uow, ctx.tenant_id, latest.document, latest.version, latest.generation + 1, now
            )
            if run is None:
                raise ConflictError("ingestion_replay_already_requested")
            spec = ingestion_job_spec(latest.document.value, latest.version.value, run.generation)
            if uow.jobs.enqueue(spec) is None:
                raise ConflictError("ingestion_replay_already_requested")
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="ingestion.ingestion_replayed",
                    resource_type="ingestion_run",
                    resource_id=str(run.id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=now,
                    attributes={
                        **who.attributes,
                        "version_id": str(run.version),
                        "generation": str(run.generation),
                    },
                )
            )
            uow.commit()
            return ReplayView(run.id, run.generation)
