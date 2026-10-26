from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.audit import AuditRecorder
from lda.shared.ports.events import EventPublisher
from lda.shared.ports.jobs import JobQueue


class UnitOfWork(Protocol):
    """One tenant-bound transaction. Module UoWs extend this with their own repositories.

    Leaving the block without `commit()` rolls back. After commit/rollback it is unusable.
    """

    @property
    def tenant_id(self) -> TenantId: ...

    @property
    def events(self) -> EventPublisher: ...

    @property
    def jobs(self) -> JobQueue: ...

    @property
    def audit(self) -> AuditRecorder: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def begin(self, ctx: ExecutionContext) -> UnitOfWork: ...
