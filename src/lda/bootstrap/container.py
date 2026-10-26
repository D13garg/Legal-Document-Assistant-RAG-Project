"""Composition root foundation (ADR-002): the only place that knows concrete implementations.

Modules never build their own dependencies. Later steps add module wiring here; the audit module
supplies `audit_factory` at step 4 (no default: audit must never be silently absent).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lda.platform.clock import RandomIdGenerator, SystemClock
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.configuration.settings import (
    DatabaseSettings,
    JobWorkerSettings,
    ObjectStorageSettings,
    RateLimitSettings,
)
from lda.platform.database.engine import create_runtime_engine
from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.maintenance import JobMaintenance
from lda.platform.database.unit_of_work import AuditRecorderFactory, SqlUnitOfWorkFactory
from lda.platform.events.codec import EventCodec
from lda.platform.events.publisher import TransactionalEventPublisher
from lda.platform.events.registry import HandlerRegistry
from lda.platform.ratelimit.factory import create_rate_limiter
from lda.platform.storage.factory import create_object_store
from lda.shared.kernel.errors import ConfigurationError
from lda.shared.ports.clock import Clock, IdGenerator
from lda.shared.ports.object_store import ObjectStore
from lda.shared.ports.rate_limiter import RateLimiter


@dataclass(frozen=True)
class PlatformContainer:
    clock: Clock
    ids: IdGenerator
    dispose: Callable[[], None]  # closes pooled DB connections on shutdown
    units_of_work: SqlUnitOfWorkFactory
    handlers: HandlerRegistry
    event_codec: EventCodec
    job_leases: JobLeaseStore
    job_maintenance: JobMaintenance
    job_settings: JobWorkerSettings
    object_store: ObjectStore
    rate_limiter: RateLimiter


def build_platform(
    config: ConfigurationLoader, *, audit_factory: AuditRecorderFactory
) -> PlatformContainer:
    # Fail fast: every slice is validated before any client or connection pool is created.
    database = config.load("database", DatabaseSettings)
    job_settings = config.load("jobs", JobWorkerSettings)
    storage = config.load("object_storage", ObjectStorageSettings)
    rate_limit = config.load("rate_limit", RateLimitSettings)
    clock, ids = SystemClock(), RandomIdGenerator()
    object_store = create_object_store(storage)
    rate_limiter = create_rate_limiter(rate_limit, clock)
    engine = create_runtime_engine(database)  # last: nothing after this can fail and leak the pool
    handlers, codec = HandlerRegistry(), EventCodec()
    units_of_work = SqlUnitOfWorkFactory(
        engine,
        clock,
        ids,
        audit_factory,
        lambda uow, jobs: TransactionalEventPublisher(handlers, codec, jobs, uow),
    )
    return PlatformContainer(
        clock=clock,
        ids=ids,
        dispose=engine.dispose,
        units_of_work=units_of_work,
        handlers=handlers,
        event_codec=codec,
        job_leases=JobLeaseStore(engine, clock),
        job_maintenance=JobMaintenance(engine, clock),
        job_settings=job_settings,
        object_store=object_store,
        rate_limiter=rate_limiter,
    )


def require_queues(platform: PlatformContainer, *queues: str) -> None:
    """Refuse a configuration under which enqueued work would never run (Step 18).

    `JobWorker` claims only the queues named in `LDA_JOBS__QUEUES`; a job on any other queue is
    never claimed and never dead-lettered, so it strands silently. The error code names only the
    queue, never a value from the environment.
    """
    for queue in queues:
        if queue not in platform.job_settings.queues:
            raise ConfigurationError(f"{queue}_queue_not_configured")
