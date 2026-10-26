from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import SQLAlchemyError

from lda.modules.documents.domain.document import (
    Document,
    DocumentStatus,
    DocumentVersion,
    VersionStatus,
)
from lda.modules.documents.domain.evidence import PurgeReceipt, UploadIntent
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.documents.infrastructure.persistence.tables import (
    documents,
    purge_receipts,
    upload_intents,
    versions,
)
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import PrincipalId, TenantId

_FAILED = "documents_persistence_failed"


def _run[T](action: Callable[[], T]) -> T:
    """Driver errors embed statement parameters (titles, ids): translate and chain them away."""
    try:
        return action()
    except SQLAlchemyError:
        raise InfrastructureError(_FAILED) from None


def _principal(value: UUID | None) -> PrincipalId | None:
    return None if value is None else PrincipalId(value)


class _Bound:
    """Every statement carries the tenant predicate and RLS enforces it again."""

    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    @property
    def _tenant(self) -> UUID:
        return self._tx.tenant_id.value

    def _check(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tx.tenant_id:
            raise InfrastructureError("documents_tenant_mismatch")


class SqlDocumentRepository(_Bound):
    def add(self, document: Document) -> None:
        self._check(document.tenant_id)
        _run(
            lambda: self._tx.session.execute(
                insert(documents).values(
                    tenant_id=self._tenant,
                    document_id=document.id.value,
                    title=document.title,
                    status=document.status.value,
                    created_by=document.created_by.value if document.created_by else None,
                    created_at=document.created_at,
                    latest_version_number=document.latest_version_number,
                    deleted_at=document.deleted_at,
                )
            )
        )

    def get(self, document_id: DocumentId, *, for_update: bool = False) -> Document | None:
        statement = select(documents).where(
            documents.c.tenant_id == self._tenant, documents.c.document_id == document_id.value
        )
        if for_update:
            statement = statement.with_for_update()
        row = _run(lambda: self._tx.session.execute(statement).first())
        if row is None:
            return None
        return Document(
            DocumentId(row.document_id),
            TenantId(row.tenant_id),
            row.title,
            DocumentStatus(row.status),
            _principal(row.created_by),
            row.created_at,
            row.latest_version_number,
            row.deleted_at,
        )

    def save(self, document: Document) -> None:
        self._check(document.tenant_id)
        statement = (
            update(documents)
            .where(
                documents.c.tenant_id == self._tenant, documents.c.document_id == document.id.value
            )
            .values(
                status=document.status.value,
                deleted_at=document.deleted_at,
                latest_version_number=document.latest_version_number,
            )
            .returning(documents.c.document_id)
        )
        if _run(lambda: self._tx.session.execute(statement).first()) is None:
            raise InfrastructureError("document_not_saved")

    def remove(self, document_id: DocumentId) -> bool:
        statement = (
            delete(documents)
            .where(
                documents.c.tenant_id == self._tenant, documents.c.document_id == document_id.value
            )
            .returning(documents.c.document_id)
        )
        return _run(lambda: self._tx.session.execute(statement).first()) is not None


def _version(row: object) -> DocumentVersion:
    r = row  # a SQLAlchemy Row: attribute access by column name
    return DocumentVersion(
        DocumentVersionId(r.version_id),  # type: ignore[attr-defined]
        DocumentId(r.document_id),  # type: ignore[attr-defined]
        TenantId(r.tenant_id),  # type: ignore[attr-defined]
        r.number,  # type: ignore[attr-defined]
        VersionStatus(r.status),  # type: ignore[attr-defined]
        r.media_type,  # type: ignore[attr-defined]
        r.size_bytes,  # type: ignore[attr-defined]
        _principal(r.created_by),  # type: ignore[attr-defined]
        r.created_at,  # type: ignore[attr-defined]
    )


class SqlVersionRepository(_Bound):
    def add(self, version: DocumentVersion) -> None:
        self._check(version.tenant_id)
        _run(
            lambda: self._tx.session.execute(
                insert(versions).values(
                    tenant_id=self._tenant,
                    version_id=version.id.value,
                    document_id=version.document_id.value,
                    number=version.number,
                    status=version.status.value,
                    media_type=version.media_type,
                    size_bytes=version.size_bytes,
                    created_by=version.created_by.value if version.created_by else None,
                    created_at=version.created_at,
                )
            )
        )

    def get(self, version_id: DocumentVersionId) -> DocumentVersion | None:
        row = _run(
            lambda: self._tx.session.execute(
                select(versions).where(
                    versions.c.tenant_id == self._tenant, versions.c.version_id == version_id.value
                )
            ).first()
        )
        return None if row is None else _version(row)

    def list_for(self, document_id: DocumentId) -> tuple[DocumentVersion, ...]:
        rows = _run(
            lambda: self._tx.session.execute(
                select(versions)
                .where(
                    versions.c.tenant_id == self._tenant,
                    versions.c.document_id == document_id.value,
                )
                .order_by(versions.c.number)
            ).all()
        )
        return tuple(_version(r) for r in rows)

    def set_outcome(self, version_id: DocumentVersionId, outcome: VersionStatus) -> bool:
        statement = (
            update(versions)
            .where(
                versions.c.tenant_id == self._tenant,
                versions.c.version_id == version_id.value,
                versions.c.status == VersionStatus.PENDING.value,
            )
            .values(status=outcome.value)
            .returning(versions.c.version_id)
        )
        return _run(lambda: self._tx.session.execute(statement).first()) is not None

    def remove_all_for(self, document_id: DocumentId) -> int:
        statement = (
            delete(versions)
            .where(
                versions.c.tenant_id == self._tenant, versions.c.document_id == document_id.value
            )
            .returning(versions.c.version_id)
        )
        return len(_run(lambda: self._tx.session.execute(statement).all()))


class SqlUploadIntentRepository(_Bound):
    def add(self, intent: UploadIntent) -> None:
        self._check(intent.tenant_id)
        _run(
            lambda: self._tx.session.execute(
                insert(upload_intents).values(
                    tenant_id=self._tenant,
                    version_id=intent.version_id.value,
                    document_id=intent.document_id.value,
                    created_at=intent.created_at,
                )
            )
        )

    def get(
        self, version_id: DocumentVersionId, *, for_update: bool = False
    ) -> UploadIntent | None:
        statement = select(upload_intents).where(
            upload_intents.c.tenant_id == self._tenant,
            upload_intents.c.version_id == version_id.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = _run(lambda: self._tx.session.execute(statement).first())
        if row is None:
            return None
        return UploadIntent(
            TenantId(row.tenant_id),
            DocumentVersionId(row.version_id),
            DocumentId(row.document_id),
            row.created_at,
        )

    def remove(self, version_id: DocumentVersionId) -> bool:
        statement = (
            delete(upload_intents)
            .where(
                upload_intents.c.tenant_id == self._tenant,
                upload_intents.c.version_id == version_id.value,
            )
            .returning(upload_intents.c.version_id)
        )
        return _run(lambda: self._tx.session.execute(statement).first()) is not None


class SqlPurgeReceiptRepository(_Bound):
    def add(self, receipt: PurgeReceipt) -> None:
        self._check(receipt.tenant_id)
        _run(
            lambda: self._tx.session.execute(
                insert(purge_receipts).values(
                    tenant_id=self._tenant,
                    document_id=receipt.document_id.value,
                    tombstoned_at=receipt.tombstoned_at,
                    purged_at=receipt.purged_at,
                    versions_purged=receipt.versions_purged,
                    version_ids=[str(v) for v in receipt.version_ids],
                    scope=receipt.scope,
                )
            )
        )

    def get(self, document_id: DocumentId) -> PurgeReceipt | None:
        row = _run(
            lambda: self._tx.session.execute(
                select(purge_receipts).where(
                    purge_receipts.c.tenant_id == self._tenant,
                    purge_receipts.c.document_id == document_id.value,
                )
            ).first()
        )
        if row is None:
            return None
        return PurgeReceipt(
            TenantId(row.tenant_id),
            DocumentId(row.document_id),
            row.tombstoned_at,
            row.purged_at,
            tuple(DocumentVersionId(UUID(v)) for v in row.version_ids),
            row.scope,
        )
