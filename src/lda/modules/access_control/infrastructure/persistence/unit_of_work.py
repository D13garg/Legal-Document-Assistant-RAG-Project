from __future__ import annotations

from types import TracebackType
from typing import Self

from lda.modules.access_control.domain.resource import ResourceRef
from lda.modules.access_control.infrastructure.persistence.epoch_repository import (
    SqlPolicyEpochRepository,
)
from lda.modules.access_control.infrastructure.persistence.grant_repository import (
    SqlGrantRepository,
)
from lda.platform.database.transaction import TenantTransaction
from lda.platform.database.unit_of_work import SqlUnitOfWork, SqlUnitOfWorkFactory
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.audit import AuditRecorder
from lda.shared.ports.events import EventPublisher
from lda.shared.ports.jobs import JobQueue


class SqlAccessControlUnitOfWork:
    """The platform's tenant-bound transaction plus Access Control's repositories.

    Grants and the policy epoch are written through the SAME transaction: they commit together.
    """

    def __init__(self, inner: SqlUnitOfWork) -> None:
        self._inner = inner
        self._grants = SqlGrantRepository(inner.transaction)
        self._policy_epochs = SqlPolicyEpochRepository(inner.transaction)

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
    def grants(self) -> SqlGrantRepository:
        return self._grants

    @property
    def policy_epochs(self) -> SqlPolicyEpochRepository:
        return self._policy_epochs

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


class SqlResourceAccessRevoker:
    """`ResourceAccessRevoker` over the CALLER's transaction (ADR-023).

    Removes every grant on one resource and advances the policy epoch exactly once if any grant was
    removed, through the same repositories Access Control's own use cases use. It never commits:
    the owning module's action commits or rolls back together with the revocation.
    """

    def __init__(self, tx: TenantTransaction) -> None:
        self._grants = SqlGrantRepository(tx)
        self._policy_epochs = SqlPolicyEpochRepository(tx)

    def revoke_all(self, resource_kind: str, resource_id: str) -> int:
        removed = self._grants.remove_all_for_resource(ResourceRef(resource_kind, resource_id))
        if removed:
            self._policy_epochs.advance()
        return removed


class SqlAccessControlUnitOfWorkFactory:
    def __init__(self, units_of_work: SqlUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def begin(self, ctx: ExecutionContext) -> SqlAccessControlUnitOfWork:
        return SqlAccessControlUnitOfWork(self._units_of_work.begin(ctx))
