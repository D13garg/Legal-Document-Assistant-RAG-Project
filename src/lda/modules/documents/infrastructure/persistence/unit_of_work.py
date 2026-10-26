from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Self

from lda.modules.documents.infrastructure.persistence.repositories import (
    SqlDocumentRepository,
    SqlPurgeReceiptRepository,
    SqlUploadIntentRepository,
    SqlVersionRepository,
)
from lda.platform.database.transaction import TenantTransaction
from lda.platform.database.unit_of_work import SqlUnitOfWork, SqlUnitOfWorkFactory
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.access_revoker import ResourceAccessRevoker
from lda.shared.ports.audit import AuditRecorder
from lda.shared.ports.events import EventPublisher
from lda.shared.ports.jobs import JobQueue

ResourceAccessRevokerFactory = Callable[[TenantTransaction], ResourceAccessRevoker]


class SqlDocumentsUnitOfWork:
    """The platform's tenant-bound transaction plus Documents' repositories and a revoker bound
    to the SAME transaction: tombstone, revocation, job, event and audit commit together."""

    def __init__(self, inner: SqlUnitOfWork, revoker: ResourceAccessRevoker) -> None:
        self._inner = inner
        tx = inner.transaction
        self._documents = SqlDocumentRepository(tx)
        self._versions = SqlVersionRepository(tx)
        self._intents = SqlUploadIntentRepository(tx)
        self._receipts = SqlPurgeReceiptRepository(tx)
        self._revoker = revoker

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
    def documents(self) -> SqlDocumentRepository:
        return self._documents

    @property
    def versions(self) -> SqlVersionRepository:
        return self._versions

    @property
    def intents(self) -> SqlUploadIntentRepository:
        return self._intents

    @property
    def receipts(self) -> SqlPurgeReceiptRepository:
        return self._receipts

    @property
    def access_revoker(self) -> ResourceAccessRevoker:
        return self._revoker

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


class SqlDocumentsUnitOfWorkFactory:
    def __init__(
        self, units_of_work: SqlUnitOfWorkFactory, revoker_factory: ResourceAccessRevokerFactory
    ) -> None:
        self._units_of_work = units_of_work
        self._revoker_factory = revoker_factory

    def begin(self, ctx: ExecutionContext) -> SqlDocumentsUnitOfWork:
        inner = self._units_of_work.begin(ctx)
        return SqlDocumentsUnitOfWork(inner, self._revoker_factory(inner.transaction))
