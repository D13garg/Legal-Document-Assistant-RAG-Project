from __future__ import annotations

from types import TracebackType
from typing import Self

from lda.modules.chunking.infrastructure.persistence.repositories import (
    SqlChunkSetRepository,
    SqlReceiptRepository,
)
from lda.platform.database.unit_of_work import SqlUnitOfWork, SqlUnitOfWorkFactory
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.audit import AuditRecorder
from lda.shared.ports.events import EventPublisher
from lda.shared.ports.jobs import JobFence, JobQueue


class SqlChunkingUnitOfWork:
    """The platform's tenant-bound transaction plus this module's repositories on the SAME
    transaction, so the fence check and the chunk-set record commit (or fail) together."""

    def __init__(self, inner: SqlUnitOfWork) -> None:
        self._inner = inner
        self._chunk_sets = SqlChunkSetRepository(inner.transaction)
        self._receipts = SqlReceiptRepository(inner.transaction)

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
    def fence(self) -> JobFence:
        return self._inner.fence

    @property
    def chunk_sets(self) -> SqlChunkSetRepository:
        return self._chunk_sets

    @property
    def receipts(self) -> SqlReceiptRepository:
        return self._receipts

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
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()


class SqlChunkingUnitOfWorkFactory:
    def __init__(self, units_of_work: SqlUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def begin(self, ctx: ExecutionContext) -> SqlChunkingUnitOfWork:
        return SqlChunkingUnitOfWork(self._units_of_work.begin(ctx))
