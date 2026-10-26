from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.domain.record import ExtractionRecord, PurgeReceipt
from lda.modules.document_processing.infrastructure.persistence.tables import (
    extractions,
    purge_receipts,
)
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId

_FAILED = "document_processing_persistence_failed"


def _run[T](action: Callable[[], T]) -> T:
    """Driver errors embed statement parameters: translate and chain them away."""
    try:
        return action()
    except SQLAlchemyError:
        raise InfrastructureError(_FAILED) from None


class _Bound:
    """Every statement carries the tenant predicate and RLS enforces it again."""

    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    @property
    def _tenant(self) -> UUID:
        return self._tx.tenant_id.value

    def _check(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tx.tenant_id:
            raise InfrastructureError("document_processing_tenant_mismatch")


def _record(row: object) -> ExtractionRecord:
    return ExtractionRecord(
        tenant_id=TenantId(row.tenant_id),  # type: ignore[attr-defined]
        document_id=DocumentId(row.document_id),  # type: ignore[attr-defined]
        version_id=VersionId(row.version_id),  # type: ignore[attr-defined]
        parser_version=row.parser_version,  # type: ignore[attr-defined]
        content_hash=row.content_hash,  # type: ignore[attr-defined]
        media_type=row.media_type,  # type: ignore[attr-defined]
        artifact_bytes=row.artifact_bytes,  # type: ignore[attr-defined]
        block_count=row.block_count,  # type: ignore[attr-defined]
        character_count=row.character_count,  # type: ignore[attr-defined]
        page_count=row.page_count,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
    )


class SqlExtractionRepository(_Bound):
    def add(self, record: ExtractionRecord) -> bool:
        self._check(record.tenant_id)
        statement = (
            pg_insert(extractions)
            .values(
                tenant_id=self._tenant,
                version_id=record.version_id.value,
                document_id=record.document_id.value,
                parser_version=record.parser_version,
                content_hash=record.content_hash,
                media_type=record.media_type,
                artifact_bytes=record.artifact_bytes,
                block_count=record.block_count,
                character_count=record.character_count,
                page_count=record.page_count,
                created_at=record.created_at,
            )
            .on_conflict_do_nothing()
            .returning(extractions.c.version_id)
        )
        return _run(lambda: self._tx.session.execute(statement).first()) is not None

    def get(self, version_id: VersionId) -> ExtractionRecord | None:
        statement = select(extractions).where(
            extractions.c.tenant_id == self._tenant,
            extractions.c.version_id == version_id.value,
        )
        row = _run(lambda: self._tx.session.execute(statement).first())
        return None if row is None else _record(row)

    def list_for(self, document_id: DocumentId) -> tuple[ExtractionRecord, ...]:
        statement = (
            select(extractions)
            .where(
                extractions.c.tenant_id == self._tenant,
                extractions.c.document_id == document_id.value,
            )
            .order_by(extractions.c.version_id)
        )
        rows = _run(lambda: self._tx.session.execute(statement).all())
        return tuple(_record(row) for row in rows)

    def remove_all_for(self, document_id: DocumentId) -> int:
        statement = (
            delete(extractions)
            .where(
                extractions.c.tenant_id == self._tenant,
                extractions.c.document_id == document_id.value,
            )
            .returning(extractions.c.version_id)
        )
        return len(_run(lambda: self._tx.session.execute(statement).all()))


class SqlReceiptRepository(_Bound):
    def add(self, receipt: PurgeReceipt) -> bool:
        self._check(receipt.tenant_id)
        statement = (
            pg_insert(purge_receipts)
            .values(
                tenant_id=self._tenant,
                document_id=receipt.document_id.value,
                purged_at=receipt.purged_at,
                versions_purged=receipt.versions_purged,
                version_ids=[str(v) for v in receipt.version_ids],
                scope=receipt.scope,
            )
            .on_conflict_do_nothing()
            .returning(purge_receipts.c.document_id)
        )
        return _run(lambda: self._tx.session.execute(statement).first()) is not None

    def get(self, document_id: DocumentId) -> PurgeReceipt | None:
        statement = select(purge_receipts).where(
            purge_receipts.c.tenant_id == self._tenant,
            purge_receipts.c.document_id == document_id.value,
        )
        row = _run(lambda: self._tx.session.execute(statement).first())
        if row is None:
            return None
        return PurgeReceipt(
            TenantId(row.tenant_id),
            DocumentId(row.document_id),
            row.purged_at,
            tuple(VersionId(UUID(v)) for v in row.version_ids),
            row.scope,
        )
