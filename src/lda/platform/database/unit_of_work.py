from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Self

from sqlalchemy import Engine

from lda.platform.database.jobs.fence import PostgresJobFence
from lda.platform.database.jobs.queue import PostgresJobQueue
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.audit import AuditRecorder
from lda.shared.ports.clock import Clock, IdGenerator
from lda.shared.ports.events import EventPublisher
from lda.shared.ports.jobs import JobFence, JobQueue
from lda.shared.ports.unit_of_work import UnitOfWork

AuditRecorderFactory = Callable[[TenantTransaction], AuditRecorder]
EventPublisherFactory = Callable[[UnitOfWork, JobQueue], EventPublisher]


class SqlUnitOfWork:
    """Tenant-bound transaction exposing the shared transactional services.

    Module UoWs compose this (or its TenantTransaction) and add their own repositories.
    """

    def __init__(
        self,
        tx: TenantTransaction,
        jobs: JobQueue,
        audit: AuditRecorder,
        events_factory: EventPublisherFactory,
        fence: JobFence,
    ) -> None:
        self._tx = tx
        self._jobs = jobs
        self._audit = audit
        self._fence = fence
        self._events = events_factory(self, jobs)

    @property
    def tenant_id(self) -> TenantId:
        return self._tx.tenant_id

    @property
    def transaction(self) -> TenantTransaction:
        return self._tx

    @property
    def events(self) -> EventPublisher:
        return self._events

    @property
    def jobs(self) -> JobQueue:
        return self._jobs

    @property
    def audit(self) -> AuditRecorder:
        return self._audit

    @property
    def fence(self) -> JobFence:
        """Proves inside this transaction that the running job's lease is still current."""
        return self._fence

    def __enter__(self) -> Self:
        self._tx.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._tx.__exit__(exc_type, exc, tb)

    def commit(self) -> None:
        self._tx.commit()

    def rollback(self) -> None:
        self._tx.rollback()


class SqlUnitOfWorkFactory:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        ids: IdGenerator,
        audit_factory: AuditRecorderFactory,
        events_factory: EventPublisherFactory,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._ids = ids
        self._audit_factory = audit_factory
        self._events_factory = events_factory

    def begin(self, ctx: ExecutionContext) -> SqlUnitOfWork:
        tx = TenantTransaction(self._engine, ctx.tenant_id)
        return SqlUnitOfWork(
            tx,
            PostgresJobQueue(tx, self._clock, self._ids),
            self._audit_factory(tx),
            self._events_factory,
            PostgresJobFence(tx, self._clock),
        )
