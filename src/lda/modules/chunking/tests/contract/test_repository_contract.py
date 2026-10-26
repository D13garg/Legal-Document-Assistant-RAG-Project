"""One behavioural contract, run against the in-memory and the SQL unit of work."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from lda.modules.chunking.application.ports import ChunkingUnitOfWorkFactory
from lda.modules.chunking.domain.chunk import Chunk, ChunkSet, Segment
from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.record import ChunkSetRecord, PurgeReceipt
from lda.modules.chunking.tests.support import ctx
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId

A, B = TenantId(UUID(int=0xA1)), TenantId(UUID(int=0xB2))
D1, D2 = DocumentId(UUID(int=0xD1)), DocumentId(UUID(int=0xD2))
V1, V2, V3 = (VersionId(UUID(int=0x60 + n)) for n in (1, 2, 3))
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def record(
    tenant: TenantId = A,
    document: DocumentId = D1,
    version: VersionId = V1,
    text: str = "clause",
) -> ChunkSetRecord:
    chunk = Chunk(0, text, (Segment(0, 0, len(text)),), ("Heading",), None, None)
    chunk_set = ChunkSet(tenant, document, version, "a" * 64, "chunk-1; max=1; min=1", (chunk,))
    return ChunkSetRecord.describe(chunk_set, NOW)[0]


def commit(factory: ChunkingUnitOfWorkFactory, context: ExecutionContext, *rows: object) -> None:
    with factory.begin(context) as uow:
        for row in rows:
            if isinstance(row, ChunkSetRecord):
                assert uow.chunk_sets.add(row)
            else:
                assert isinstance(row, PurgeReceipt)
                assert uow.receipts.add(row)
        uow.commit()


def test_record_round_trips_exactly(factory: ChunkingUnitOfWorkFactory) -> None:
    stored = record()
    commit(factory, ctx(A), stored)
    with factory.begin(ctx(A)) as uow:
        assert uow.chunk_sets.get(V1) == stored
        assert uow.chunk_sets.get(V2) is None


def test_add_is_insert_if_absent_and_never_replaces(factory: ChunkingUnitOfWorkFactory) -> None:
    first = record(text="first")
    commit(factory, ctx(A), first)
    with factory.begin(ctx(A)) as uow:
        assert uow.chunk_sets.add(record(text="second")) is False
        uow.commit()
    with factory.begin(ctx(A)) as uow:
        assert uow.chunk_sets.get(V1) == first


def test_uncommitted_work_is_discarded(factory: ChunkingUnitOfWorkFactory) -> None:
    with factory.begin(ctx(A)) as uow:
        assert uow.chunk_sets.add(record())
    with factory.begin(ctx(A)) as uow:
        assert uow.chunk_sets.get(V1) is None


def test_exception_inside_the_block_discards_work(factory: ChunkingUnitOfWorkFactory) -> None:
    with pytest.raises(RuntimeError), factory.begin(ctx(A)) as uow:
        uow.chunk_sets.add(record())
        raise RuntimeError("boom")
    with factory.begin(ctx(A)) as uow:
        assert uow.chunk_sets.get(V1) is None


def test_list_is_scoped_to_document_and_ordered(factory: ChunkingUnitOfWorkFactory) -> None:
    commit(factory, ctx(A), record(version=V2), record(version=V1), record(document=D2, version=V3))
    with factory.begin(ctx(A)) as uow:
        assert [r.version_id for r in uow.chunk_sets.list_for(D1)] == [V1, V2]
        assert [r.version_id for r in uow.chunk_sets.list_for(D2)] == [V3]
        assert uow.chunk_sets.list_for(DocumentId(UUID(int=0xFF))) == ()


def test_tenants_never_see_each_other(factory: ChunkingUnitOfWorkFactory) -> None:
    commit(factory, ctx(A), record())
    with factory.begin(ctx(B)) as uow:
        assert uow.chunk_sets.get(V1) is None
        assert uow.chunk_sets.list_for(D1) == ()
        assert uow.chunk_sets.remove_all_for(D1) == 0
        assert uow.chunk_sets.add(record(tenant=B)) is True  # the same version id: another row
        uow.commit()
    with factory.begin(ctx(A)) as uow:
        assert uow.chunk_sets.get(V1) is not None


def test_a_record_of_another_tenant_cannot_be_written(factory: ChunkingUnitOfWorkFactory) -> None:
    with factory.begin(ctx(A)) as uow, pytest.raises(InfrastructureError):
        uow.chunk_sets.add(record(tenant=B))
    with factory.begin(ctx(A)) as uow, pytest.raises(InfrastructureError):
        uow.receipts.add(PurgeReceipt(B, D1, NOW, ()))


def test_remove_all_only_removes_the_documents_rows(factory: ChunkingUnitOfWorkFactory) -> None:
    commit(factory, ctx(A), record(version=V1), record(version=V2), record(document=D2, version=V3))
    with factory.begin(ctx(A)) as uow:
        assert uow.chunk_sets.remove_all_for(D1) == 2
        uow.commit()
    with factory.begin(ctx(A)) as uow:
        assert uow.chunk_sets.list_for(D1) == ()
        assert uow.chunk_sets.get(V3) is not None


def test_receipt_round_trips_and_is_insert_if_absent(factory: ChunkingUnitOfWorkFactory) -> None:
    receipt = PurgeReceipt(A, D1, NOW, (V1, V2))
    commit(factory, ctx(A), receipt)
    with factory.begin(ctx(A)) as uow:
        assert uow.receipts.get(D1) == receipt
        assert uow.receipts.add(replace(receipt, version_ids=())) is False
        assert uow.receipts.get(D1) == receipt
        assert uow.receipts.get(D2) is None
    with factory.begin(ctx(B)) as uow:
        assert uow.receipts.get(D1) is None


def test_receipt_of_a_document_without_versions(factory: ChunkingUnitOfWorkFactory) -> None:
    commit(factory, ctx(A), PurgeReceipt(A, D2, NOW, ()))
    with factory.begin(ctx(A)) as uow:
        stored = uow.receipts.get(D2)
        assert stored is not None and stored.versions_purged == 0


def test_the_lifecycle_lock_can_be_taken_repeatedly_in_one_transaction(
    factory: ChunkingUnitOfWorkFactory,
) -> None:
    with factory.begin(ctx(A)) as uow:
        uow.chunk_sets.lock_document(D1)
        uow.chunk_sets.lock_document(D1)  # re-entrant for the holder
        uow.chunk_sets.lock_document(D2)
