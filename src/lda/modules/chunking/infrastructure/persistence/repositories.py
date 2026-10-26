from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.record import ChunkSetRecord, PurgeReceipt
from lda.modules.chunking.infrastructure.persistence.tables import chunk_sets, purge_receipts
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId

_FAILED = "chunking_persistence_failed"
_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))")


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
            raise InfrastructureError("chunking_tenant_mismatch")


def _record(row: object) -> ChunkSetRecord:
    return ChunkSetRecord(
        tenant_id=TenantId(row.tenant_id),  # type: ignore[attr-defined]
        document_id=DocumentId(row.document_id),  # type: ignore[attr-defined]
        version_id=VersionId(row.version_id),  # type: ignore[attr-defined]
        extraction_hash=row.extraction_hash,  # type: ignore[attr-defined]
        chunker_version=row.chunker_version,  # type: ignore[attr-defined]
        content_hash=row.content_hash,  # type: ignore[attr-defined]
        artifact_bytes=row.artifact_bytes,  # type: ignore[attr-defined]
        chunk_count=row.chunk_count,  # type: ignore[attr-defined]
        character_count=row.character_count,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
    )


class SqlChunkSetRepository(_Bound):
    def add(self, record: ChunkSetRecord) -> bool:
        self._check(record.tenant_id)
        statement = (
            pg_insert(chunk_sets)
            .values(
                tenant_id=self._tenant,
                version_id=record.version_id.value,
                document_id=record.document_id.value,
                extraction_hash=record.extraction_hash,
                chunker_version=record.chunker_version,
                content_hash=record.content_hash,
                artifact_bytes=record.artifact_bytes,
                chunk_count=record.chunk_count,
                character_count=record.character_count,
                created_at=record.created_at,
            )
            .on_conflict_do_nothing()
            .returning(chunk_sets.c.version_id)
        )
        return _run(lambda: self._tx.session.execute(statement).first()) is not None

    def get(self, version_id: VersionId) -> ChunkSetRecord | None:
        statement = select(chunk_sets).where(
            chunk_sets.c.tenant_id == self._tenant,
            chunk_sets.c.version_id == version_id.value,
        )
        row = _run(lambda: self._tx.session.execute(statement).first())
        return None if row is None else _record(row)

    def list_for(self, document_id: DocumentId) -> tuple[ChunkSetRecord, ...]:
        statement = (
            select(chunk_sets)
            .where(
                chunk_sets.c.tenant_id == self._tenant,
                chunk_sets.c.document_id == document_id.value,
            )
            .order_by(chunk_sets.c.version_id)
        )
        rows = _run(lambda: self._tx.session.execute(statement).all())
        return tuple(_record(row) for row in rows)

    def remove_all_for(self, document_id: DocumentId) -> int:
        statement = (
            delete(chunk_sets)
            .where(
                chunk_sets.c.tenant_id == self._tenant,
                chunk_sets.c.document_id == document_id.value,
            )
            .returning(chunk_sets.c.version_id)
        )
        return len(_run(lambda: self._tx.session.execute(statement).all()))

    def lock_document(self, document_id: DocumentId) -> None:
        """A transaction-scoped advisory lock named by tenant and document: released at commit or
        rollback, never held across transactions, and never shared with another tenant's name."""
        name = f"chunking:{self._tenant}:{document_id.value}"
        _run(lambda: self._tx.session.execute(_LOCK, {"name": name}))


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
