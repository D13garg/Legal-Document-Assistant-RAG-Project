from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.exc import SQLAlchemyError

from lda.modules.conversations.domain import codes
from lda.modules.conversations.infrastructure.persistence.repositories import (
    SqlConversationRepository,
    SqlDeletionReceiptRepository,
)
from lda.platform.database.unit_of_work import SqlUnitOfWork, SqlUnitOfWorkFactory
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.audit import AuditRecorder
from lda.shared.ports.events import EventPublisher
from lda.shared.ports.jobs import JobQueue


class SqlConversationsUnitOfWork:
    """The platform's tenant-bound transaction plus this module's repositories."""

    def __init__(self, inner: SqlUnitOfWork) -> None:
        self._inner = inner
        self._conversations = SqlConversationRepository(inner.transaction)
        self._receipts = SqlDeletionReceiptRepository(inner.transaction)

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
    def conversations(self) -> SqlConversationRepository:
        return self._conversations

    @property
    def receipts(self) -> SqlDeletionReceiptRepository:
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
        try:
            self._inner.commit()
        except SQLAlchemyError:
            # A deferred constraint can fail here; the driver error embeds statement parameters.
            raise InfrastructureError(codes.PERSISTENCE_FAILED) from None

    def rollback(self) -> None:
        self._inner.rollback()


class SqlConversationsUnitOfWorkFactory:
    def __init__(self, units_of_work: SqlUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def begin(self, ctx: ExecutionContext) -> SqlConversationsUnitOfWork:
        return SqlConversationsUnitOfWork(self._units_of_work.begin(ctx))
