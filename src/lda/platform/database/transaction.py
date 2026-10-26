"""Transactions whose database scope is fixed at construction and enforced by RLS (ADR-013)."""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Self, cast

from sqlalchemy import CursorResult, Engine, text
from sqlalchemy.orm import Session

from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId

TENANT_GUC = "app.tenant_id"
SYSTEM_SCOPE_GUC = "app.system_scope"
JOB_QUEUE_SCOPE = "job_queue"


class _ScopedTransaction:
    """One database transaction with transaction-local scope settings applied before any work.

    Lifecycle: enter -> use `session` -> commit() or rollback() (or leave the block, which rolls
    back). After commit/rollback the session is closed: nothing can run outside the scope.
    """

    def __init__(self, engine: Engine, settings: Mapping[str, str]) -> None:
        self._engine = engine
        self._settings = dict(settings)
        self._session: Session | None = None
        self._finished = False

    def __enter__(self) -> Self:
        if self._session is not None or self._finished:
            raise InfrastructureError("transaction_reused")
        session = Session(self._engine, expire_on_commit=False)
        try:
            for name, value in self._settings.items():
                session.execute(
                    text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
                )
        except BaseException:
            session.close()
            raise
        self._session = session
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            self.rollback()

    @property
    def session(self) -> Session:
        if self._session is None:
            raise InfrastructureError("transaction_not_open")
        return self._session

    def execute_count(self, statement: object, params: Mapping[str, object]) -> int:
        """Run a DML statement and return the affected row count."""
        result = self.session.execute(statement, dict(params))  # type: ignore[call-overload]
        return cast(CursorResult[tuple[object, ...]], result).rowcount

    def commit(self) -> None:
        session = self.session
        try:
            session.commit()
        finally:
            self._close()

    def rollback(self) -> None:
        session = self.session
        try:
            session.rollback()
        finally:
            self._close()

    def _close(self) -> None:
        if self._session is not None:
            self._session.close()
        self._session = None
        self._finished = True


class TenantTransaction(_ScopedTransaction):
    """A transaction that can only see and write rows of one tenant."""

    def __init__(self, engine: Engine, tenant_id: TenantId) -> None:
        super().__init__(engine, {TENANT_GUC: str(tenant_id)})
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id


class SystemQueueTransaction(_ScopedTransaction):
    """The single sanctioned cross-tenant path: job-queue maintenance on `platform.jobs` only."""

    def __init__(self, engine: Engine) -> None:
        super().__init__(engine, {SYSTEM_SCOPE_GUC: JOB_QUEUE_SCOPE})
