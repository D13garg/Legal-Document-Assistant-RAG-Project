"""Postgres `GenerationRecordRepository` (ADR-016): insert-only, tenant-bound, content-free.

Every statement carries the tenant predicate and forced RLS enforces it again. Driver errors embed
statement parameters, so every database failure is translated and chained away; nothing here
logs, and no error carries a value from a record.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from lda.modules.citations.domain.codes import RECORD_EXISTS
from lda.modules.citations.domain.evidence import Citation
from lda.modules.citations.domain.ids import CitedDocumentId, CitedVersionId
from lda.modules.citations.domain.record import GenerationOutcome, GenerationRecord
from lda.modules.citations.domain.source import CitationSource
from lda.modules.citations.infrastructure.persistence.tables import (
    generation_record_citations as citation_rows,
)
from lda.modules.citations.infrastructure.persistence.tables import generation_records as records
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import ConflictError, InfrastructureError, ValidationError
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId

FAILED = "citations_persistence_failed"
CORRUPT = "citations_record_corrupt"
TENANT_MISMATCH = "citations_tenant_mismatch"


def _run[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except SQLAlchemyError:
        raise InfrastructureError(FAILED) from None


class SqlGenerationRecordRepository:
    """Bound to one open `TenantTransaction`; the caller commits or rolls back."""

    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    def add(self, record: GenerationRecord) -> None:
        if record.tenant_id != self._tx.tenant_id:
            raise InfrastructureError(TENANT_MISMATCH)
        tenant = record.tenant_id.value
        head = (
            pg_insert(records)
            .values(
                tenant_id=tenant,
                record_id=record.record_id,
                principal_id=record.principal_id.value,
                request_id=record.request_id.value,
                created_at=record.created_at,
                config_id=record.config_id,
                outcome=record.outcome.value,
                template_id=record.template_id,
                template_version=record.template_version,
                model_id=record.model_id,
                request_sha256=record.request_sha256,
                evidence_fingerprint=record.evidence_fingerprint,
                citation_count=len(record.citations),
            )
            .on_conflict_do_nothing()
            .returning(records.c.record_id)
        )
        if _run(lambda: self._tx.session.execute(head).first()) is None:
            raise ConflictError(RECORD_EXISTS)
        if record.citations:
            rows = [
                {
                    "tenant_id": tenant,
                    "record_id": record.record_id,
                    "marker": c.marker,
                    "position": index,
                    "document_id": c.source.document_id.value,
                    "version_id": c.source.version_id.value,
                    "chunk_id": c.source.chunk_id,
                    "ordinal": c.source.ordinal,
                    "text_hash": c.source.text_hash,
                    "page_start": c.source.page_start,
                    "page_end": c.source.page_end,
                }
                for index, c in enumerate(record.citations)
            ]
            _run(lambda: self._tx.session.execute(pg_insert(citation_rows), rows))

    def get(self, tenant_id: TenantId, record_id: UUID) -> GenerationRecord | None:
        if tenant_id != self._tx.tenant_id:
            raise InfrastructureError(TENANT_MISMATCH)
        tenant = tenant_id.value
        head = select(records).where(
            records.c.tenant_id == tenant, records.c.record_id == record_id
        )
        row = _run(lambda: self._tx.session.execute(head).first())
        if row is None:
            return None
        tail = (
            select(citation_rows)
            .where(citation_rows.c.tenant_id == tenant, citation_rows.c.record_id == record_id)
            .order_by(citation_rows.c.position)
        )
        cited = _run(lambda: self._tx.session.execute(tail).all())
        try:
            if len(cited) != row.citation_count:
                raise InfrastructureError(CORRUPT)
            return GenerationRecord(
                record_id=row.record_id,
                tenant_id=TenantId(row.tenant_id),
                principal_id=PrincipalId(row.principal_id),
                request_id=RequestId(row.request_id),
                created_at=row.created_at,
                config_id=row.config_id,
                outcome=GenerationOutcome(row.outcome),
                template_id=row.template_id,
                template_version=row.template_version,
                model_id=row.model_id,
                request_sha256=row.request_sha256,
                evidence_fingerprint=row.evidence_fingerprint,
                citations=tuple(
                    Citation(
                        c.marker,
                        CitationSource(
                            document_id=CitedDocumentId(c.document_id),
                            version_id=CitedVersionId(c.version_id),
                            chunk_id=c.chunk_id,
                            ordinal=c.ordinal,
                            text_hash=c.text_hash,
                            page_start=c.page_start,
                            page_end=c.page_end,
                        ),
                    )
                    for c in cited
                ),
            )
        except (ValidationError, ValueError):
            raise InfrastructureError(CORRUPT) from None
