"""The production `GenerationRecordRepository` adapter (ADR-016)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from lda.modules.citations.application.ports import GenerationRecordRepository
from lda.modules.citations.domain.record import GenerationRecord
from lda.modules.citations.infrastructure.persistence.repositories import (
    FAILED,
    SqlGenerationRecordRepository,
)
from lda.platform.database.transaction import TenantTransaction
from lda.platform.database.unit_of_work import SqlUnitOfWork, SqlUnitOfWorkFactory
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId

READ_PURPOSE = "citations.read_generation_record"


def bind_generation_records(tx: TenantTransaction) -> GenerationRecordRepository:
    """The repository bound to a caller's transaction (ADR-016 amendment 2, ADR-029).

    A caller that must commit a record atomically with other work (grounded generation commits it
    with its audit fact) passes this function to its own unit of work. It lives here because this
    adapter is the only place allowed to instantiate the repository (AG055).
    """
    return SqlGenerationRecordRepository(tx)


class UnitOfWorkGenerationRecords:
    """The production `GenerationRecordRepository`: one tenant-bound transaction per call.

    `add` commits on its own: a record is written after the generation, and a failed write
    raises, so a caller can refuse to show an output it could not record. A caller that needs
    the record to commit atomically with other work uses `SqlGenerationRecordRepository` on
    that work's transaction instead.
    """

    def __init__(self, units_of_work: SqlUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def add(self, record: GenerationRecord) -> None:
        ctx = TenantContext(record.tenant_id, record.principal_id, record.request_id)
        with self._units_of_work.begin(ctx) as uow:
            SqlGenerationRecordRepository(uow.transaction).add(record)
            self._commit(uow)

    def get(self, tenant_id: TenantId, record_id: UUID) -> GenerationRecord | None:
        with self._units_of_work.begin(SystemContext(tenant_id, READ_PURPOSE)) as uow:
            return SqlGenerationRecordRepository(uow.transaction).get(tenant_id, record_id)

    @staticmethod
    def _commit(uow: SqlUnitOfWork) -> None:
        """The deferred completeness trigger fires at commit: translate that failure too."""
        try:
            uow.commit()
        except SQLAlchemyError:
            raise InfrastructureError(FAILED) from None
