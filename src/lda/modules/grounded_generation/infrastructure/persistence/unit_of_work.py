"""The production `GroundedUnitOfWorkFactory` (ADR-029).

One tenant-bound transaction carries both the insert-only `GenerationRecord` write and the audit
fact, so the two commit or roll back together. The record repository is built by the caller-supplied
function (the composition root binds citations' PostgreSQL repository), so this module never
imports another module's infrastructure. The deferred completeness trigger fires at commit: that
failure is translated like every other database failure, and no driver message is kept.
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Self

from sqlalchemy.exc import SQLAlchemyError

from lda.modules.citations.api import GenerationRecordRepository
from lda.modules.grounded_generation.domain import codes
from lda.platform.database.transaction import TenantTransaction
from lda.platform.database.unit_of_work import SqlUnitOfWork, SqlUnitOfWorkFactory
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.audit import AuditRecorder
from lda.shared.ports.events import EventPublisher
from lda.shared.ports.jobs import JobQueue

RecordsFor = Callable[[TenantTransaction], GenerationRecordRepository]


class SqlGroundedUnitOfWork:
    def __init__(self, inner: SqlUnitOfWork, records: GenerationRecordRepository) -> None:
        self._inner = inner
        self._records = records

    @property
    def tenant_id(self) -> TenantId:
        return self._inner.tenant_id

    @property
    def events(self) -> EventPublisher:
        return self._inner.events

    @property
    def jobs(self) -> JobQueue:
        return self._inner.jobs

    @property
    def audit(self) -> AuditRecorder:
        return self._inner.audit

    @property
    def records(self) -> GenerationRecordRepository:
        return self._records

    def __enter__(self) -> Self:
        self._inner.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._inner.__exit__(exc_type, exc, tb)

    def commit(self) -> None:
        try:
            self._inner.commit()
        except SQLAlchemyError:
            raise InfrastructureError(codes.PERSISTENCE_FAILED) from None

    def rollback(self) -> None:
        self._inner.rollback()


class SqlGroundedUnitOfWorkFactory:
    def __init__(self, units_of_work: SqlUnitOfWorkFactory, records_for: RecordsFor) -> None:
        self._units_of_work = units_of_work
        self._records_for = records_for

    def begin(self, ctx: TenantContext) -> SqlGroundedUnitOfWork:
        inner = self._units_of_work.begin(ctx)
        return SqlGroundedUnitOfWork(inner, self._records_for(inner.transaction))
