from __future__ import annotations

from collections.abc import Callable, Sequence
from itertools import batched
from uuid import UUID

from sqlalchemy import ARRAY, Uuid, bindparam, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from lda.modules.retrieval.domain.embedding import EmbeddingIdentity, Vector, to_literal
from lda.modules.retrieval.domain.ids import ChunkId, DocumentId, VersionId
from lda.modules.retrieval.domain.record import IndexedChunk, IndexedVersion, PurgeReceipt
from lda.modules.retrieval.domain.selection import Candidate
from lda.modules.retrieval.infrastructure.persistence.tables import (
    chunk_vectors,
    index_versions,
    purge_receipts,
)
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import InfrastructureError, ValidationError
from lda.shared.kernel.ids import TenantId

_FAILED = "retrieval_persistence_failed"
_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))")
_INSERT_VECTOR = text(
    "INSERT INTO retrieval.chunk_vectors "
    "(tenant_id, index_id, chunk_id, document_id, version_id, ordinal, text_hash, embedding) "
    "VALUES (:tenant_id, :index_id, :chunk_id, :document_id, :version_id, :ordinal, :text_hash, "
    "CAST(:embedding AS public.vector))"
)
# Every statement names the tenant, the index identity and the exact document set; RLS enforces the
# tenant again. Cosine distance is the pgvector operator `<=>`, schema-qualified.
_NEAREST = text(
    "SELECT document_id, version_id, chunk_id, ordinal, text_hash, "
    "1 - (embedding OPERATOR(public.<=>) CAST(:query AS public.vector)) AS similarity "
    "FROM retrieval.chunk_vectors "
    "WHERE tenant_id = :tenant AND index_id = :index_id AND document_id = ANY(:documents) "
    "ORDER BY embedding OPERATOR(public.<=>) CAST(:query AS public.vector), chunk_id "
    "LIMIT :limit OFFSET :offset"
).bindparams(bindparam("documents", type_=ARRAY(Uuid)))
_INSERT_BATCH = 500


def _run[T](action: Callable[[], T]) -> T:
    """Driver errors embed statement parameters (vectors): translate and chain them away."""
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
            raise InfrastructureError("retrieval_tenant_mismatch")


def _version(row: object) -> IndexedVersion:
    return IndexedVersion(
        tenant_id=TenantId(row.tenant_id),  # type: ignore[attr-defined]
        document_id=DocumentId(row.document_id),  # type: ignore[attr-defined]
        version_id=VersionId(row.version_id),  # type: ignore[attr-defined]
        embedding=EmbeddingIdentity(
            row.embedding_provider,  # type: ignore[attr-defined]
            row.embedding_model,  # type: ignore[attr-defined]
            row.embedding_version,  # type: ignore[attr-defined]
            row.dimensions,  # type: ignore[attr-defined]
        ),
        chunker_version=row.chunker_version,  # type: ignore[attr-defined]
        source_hash=row.source_hash,  # type: ignore[attr-defined]
        chunk_count=row.chunk_count,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
    )


class SqlVectorIndexRepository(_Bound):
    def add_version(self, record: IndexedVersion, chunks: Sequence[IndexedChunk]) -> bool:
        self._check(record.tenant_id)
        if len(chunks) != record.chunk_count:
            raise ValidationError("index_record_counts_invalid")
        statement = (
            pg_insert(index_versions)
            .values(
                tenant_id=self._tenant,
                version_id=record.version_id.value,
                index_id=record.index_id,
                document_id=record.document_id.value,
                embedding_provider=record.embedding.provider,
                embedding_model=record.embedding.model,
                embedding_version=record.embedding.version,
                dimensions=record.embedding.dimensions,
                chunker_version=record.chunker_version,
                source_hash=record.source_hash,
                chunk_count=record.chunk_count,
                created_at=record.created_at,
            )
            .on_conflict_do_nothing()
            .returning(index_versions.c.version_id)
        )
        if _run(lambda: self._tx.session.execute(statement).first()) is None:
            return False
        for batch in batched(chunks, _INSERT_BATCH):
            rows = [
                {
                    "tenant_id": self._tenant,
                    "index_id": record.index_id,
                    "chunk_id": c.chunk_id.value,
                    "document_id": record.document_id.value,
                    "version_id": record.version_id.value,
                    "ordinal": c.ordinal,
                    "text_hash": c.text_hash,
                    "embedding": to_literal(c.vector),
                }
                for c in batch
            ]
            _run(lambda rows=rows: self._tx.session.execute(_INSERT_VECTOR, rows))  # type: ignore[misc]
        return True

    def get_version(self, version_id: VersionId, index_id: str) -> IndexedVersion | None:
        statement = select(index_versions).where(
            index_versions.c.tenant_id == self._tenant,
            index_versions.c.version_id == version_id.value,
            index_versions.c.index_id == index_id,
        )
        row = _run(lambda: self._tx.session.execute(statement).first())
        return None if row is None else _version(row)

    def versions_of(
        self, documents: Sequence[DocumentId]
    ) -> dict[DocumentId, tuple[VersionId, ...]]:
        found: dict[DocumentId, set[VersionId]] = {}
        for batch in batched(documents, 1000):
            statement = (
                select(index_versions.c.document_id, index_versions.c.version_id)
                .where(
                    index_versions.c.tenant_id == self._tenant,
                    index_versions.c.document_id.in_([d.value for d in batch]),
                )
                .distinct()
            )
            for row in _run(lambda s=statement: self._tx.session.execute(s).all()):  # type: ignore[misc]
                found.setdefault(DocumentId(row.document_id), set()).add(VersionId(row.version_id))
        return {d: tuple(sorted(v, key=str)) for d, v in found.items()}

    def nearest(
        self,
        index_id: str,
        documents: Sequence[DocumentId],
        query: Vector,
        limit: int,
        offset: int,
    ) -> tuple[Candidate, ...]:
        if limit < 1 or offset < 0:
            raise ValidationError("search_page_invalid")
        params = {
            "tenant": self._tenant,
            "index_id": index_id,
            "documents": [d.value for d in documents],
            "query": to_literal(query),
            "limit": limit,
            "offset": offset,
        }
        rows = _run(lambda: self._tx.session.execute(_NEAREST, params).all())
        return tuple(
            Candidate(
                DocumentId(r.document_id),
                VersionId(r.version_id),
                ChunkId(r.chunk_id),
                r.ordinal,
                r.text_hash,
                float(r.similarity),
            )
            for r in rows
        )

    def list_for(self, document_id: DocumentId) -> tuple[VersionId, ...]:
        statement = (
            select(index_versions.c.version_id)
            .where(
                index_versions.c.tenant_id == self._tenant,
                index_versions.c.document_id == document_id.value,
            )
            .distinct()
            .order_by(index_versions.c.version_id)
        )
        rows = _run(lambda: self._tx.session.execute(statement).all())
        return tuple(VersionId(r.version_id) for r in rows)

    def remove_all_for(self, document_id: DocumentId) -> int:
        vectors = delete(chunk_vectors).where(
            chunk_vectors.c.tenant_id == self._tenant,
            chunk_vectors.c.document_id == document_id.value,
        )
        versions = (
            delete(index_versions)
            .where(
                index_versions.c.tenant_id == self._tenant,
                index_versions.c.document_id == document_id.value,
            )
            .returning(index_versions.c.version_id)
        )
        _run(lambda: self._tx.session.execute(vectors))
        return len(_run(lambda: self._tx.session.execute(versions).all()))

    def lock_document(self, document_id: DocumentId) -> None:
        """A transaction-scoped advisory lock named by tenant and document: released at commit or
        rollback, never held across transactions, and never shared with another tenant's name."""
        name = f"retrieval:{self._tenant}:{document_id.value}"
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
                versions_purged=len(receipt.version_ids),
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
