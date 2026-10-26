"""Ingestion wiring (ADR-010, ADR-011).

Order: `build_ingestion(platform, settings, scanner=..., stages=...)` BEFORE `build_docs` (Documents
imports ingestion's API and registers its event handlers). Pass `INGESTION_MIGRATIONS` to
`migrate.upgrade`; configure the `ingestion` queue (and `events`) in `LDA_JOBS__QUEUES`; register
`container.job_handlers` with the worker. Production composition REJECTS an empty list of stages;
the scanner and every stage are supplied by the caller (Step 6 has only fakes: ingestion.testing).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from lda.bootstrap.container import PlatformContainer
from lda.modules.ingestion.api import (
    INGESTION_QUEUE,
    RUN_JOB,
    WATCHDOG_JOB,
    AcknowledgeOutcome,
    CancelIngestion,
    ContentScanner,
    ExecuteRun,
    GetIngestionMetrics,
    GetIngestionStatus,
    IngestionCompleted,
    IngestionFailed,
    IngestionQuarantined,
    Pipeline,
    PipelineStage,
    PurgeIngestionRecords,
    ReplayIngestion,
    RunJobHandler,
    RunStarter,
    RunWatchdog,
    WatchdogJobHandler,
)
from lda.modules.ingestion.config import IngestionSettings
from lda.modules.ingestion.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.ingestion.infrastructure.persistence.unit_of_work import (
    SqlIngestionUnitOfWorkFactory,
)
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError
from lda.shared.ports.jobs import JobHandler

INGESTION_MIGRATIONS = MIGRATIONS_VERSIONS
"""Pass as `extra_locations` to `platform.database.migrate.upgrade` (creates the schema)."""


@dataclass(frozen=True)
class IngestionContainer:
    execute_run: ExecuteRun
    run_watchdog: RunWatchdog
    replay_ingestion: ReplayIngestion
    acknowledge_outcome: AcknowledgeOutcome
    cancel_ingestion: CancelIngestion
    purge_ingestion_records: PurgeIngestionRecords
    get_ingestion_status: GetIngestionStatus
    get_ingestion_metrics: GetIngestionMetrics
    job_handlers: Mapping[str, JobHandler]
    """Register with the job worker. The `ingestion` queue must exist in `LDA_JOBS__QUEUES`."""


def load_ingestion_settings(config: ConfigurationLoader) -> IngestionSettings:
    return config.load("ingestion", IngestionSettings)


def build_ingestion(
    platform: PlatformContainer,
    settings: IngestionSettings,
    *,
    scanner: ContentScanner,
    stages: Sequence[PipelineStage],
) -> IngestionContainer:
    jobs = platform.job_settings
    if INGESTION_QUEUE not in jobs.queues:
        raise ConfigurationError("ingestion_queue_not_configured")
    if settings.watchdog_delay_seconds <= jobs.lease_seconds + jobs.backoff_cap_seconds:
        # A job can legitimately be silent for a lease plus a backoff: the watchdog must outlast it.
        raise ConfigurationError("ingestion_watchdog_delay_too_short")
    for event_type in (IngestionCompleted, IngestionFailed, IngestionQuarantined):
        platform.event_codec.register(event_type)
    pipeline = Pipeline(scanner, stages)  # rejects an empty plan
    units_of_work = SqlIngestionUnitOfWorkFactory(platform.units_of_work)
    clock, ids = platform.clock, platform.ids
    delay = timedelta(seconds=settings.watchdog_delay_seconds)
    starter = RunStarter(pipeline, delay)
    execute_run = ExecuteRun(units_of_work, pipeline, starter, clock, ids)
    watchdog = RunWatchdog(units_of_work, clock, ids, delay, settings.max_outcome_redeliveries)
    return IngestionContainer(
        execute_run=execute_run,
        run_watchdog=watchdog,
        replay_ingestion=ReplayIngestion(units_of_work, starter, clock),
        acknowledge_outcome=AcknowledgeOutcome(units_of_work, clock),
        cancel_ingestion=CancelIngestion(units_of_work, clock),
        purge_ingestion_records=PurgeIngestionRecords(units_of_work),
        get_ingestion_status=GetIngestionStatus(units_of_work),
        get_ingestion_metrics=GetIngestionMetrics(units_of_work),
        job_handlers={
            RUN_JOB: RunJobHandler(execute_run),
            WATCHDOG_JOB: WatchdogJobHandler(watchdog),
        },
    )
