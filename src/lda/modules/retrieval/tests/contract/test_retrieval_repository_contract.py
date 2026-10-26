"""One behavioural contract, run against the in-memory and the SQL unit of work."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from lda.modules.retrieval.application.ports import RetrievalUnitOfWorkFactory
from lda.modules.retrieval.domain.embedding import EmbeddingIdentity, checked_vector
from lda.modules.retrieval.domain.ids import ChunkId, DocumentId, VersionId
from lda.modules.retrieval.domain.record import IndexedChunk, IndexedVersion, PurgeReceipt
from lda.modules.retrieval.testing import FAKE_IDENTITY, FakeEmbedder
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import TenantId

A, B = TenantId(UUID(int=0xA1)), TenantId(UUID(int=0xB2))
D1, D2 = DocumentId(UUID(int=0xD1)), DocumentId(UUID(int=0xD2))
V1, V2 = VersionId(UUID(int=0x61)), VersionId(UUID(int=0x62))
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
HASH = "a" * 64
EMBED = FakeEmbedder()


def system(tenant: TenantId = A) -> SystemContext:
    return SystemContext(tenant, "retrieval-contract")


def vector(text: str) -> tuple[float, ...]:
    return checked_vector(EMBED.embed_query(text))


def version_record(
    tenant: TenantId = A,
    document: DocumentId = D1,
    version: VersionId = V1,
    count: int = 2,
    identity: EmbeddingIdentity = FAKE_IDENTITY,
) -> IndexedVersion:
    return IndexedVersion(
        tenant, document, version, identity, "chunk-1; max=1; min=1", HASH, count, NOW
    )


def chunks(base: int, *texts: str) -> list[IndexedChunk]:
    return [
        IndexedChunk(ChunkId(UUID(int=base + i)), i, HASH, vector(t)) for i, t in enumerate(texts)
    ]


def commit(
    factory: RetrievalUnitOfWorkFactory,
    tenant: TenantId = A,
    record: IndexedVersion | None = None,
    rows: list[IndexedChunk] | None = None,
) -> bool:
    with factory.begin(system(tenant)) as uow:
        added = uow.index.add_version(
            record or version_record(tenant), rows or chunks(0x100, "payment due", "governing law")
        )
        uow.commit()
        return added


def test_a_version_and_its_vectors_round_trip(factory: RetrievalUnitOfWorkFactory) -> None:
    assert commit(factory)
    with factory.begin(system()) as uow:
        got = uow.index.get_version(V1, FAKE_IDENTITY.index_id)
        assert got == version_record()
        assert uow.index.get_version(V2, FAKE_IDENTITY.index_id) is None
        assert uow.index.get_version(V1, "0" * 32) is None


def test_a_recorded_version_is_never_replaced(factory: RetrievalUnitOfWorkFactory) -> None:
    assert commit(factory)
    assert not commit(factory, rows=chunks(0x200, "payment due", "governing law"))
    with factory.begin(system()) as uow:
        found = uow.index.nearest(FAKE_IDENTITY.index_id, [D1], vector("payment due"), 10, 0)
    assert {c.chunk_id.value.int for c in found} == {0x100, 0x101}


def test_a_chunk_count_mismatch_is_refused_and_nothing_is_stored(
    factory: RetrievalUnitOfWorkFactory,
) -> None:
    with pytest.raises(ValidationError), factory.begin(system()) as uow:
        uow.index.add_version(version_record(count=3), chunks(0x100, "a b", "c d"))
    with factory.begin(system()) as uow:
        assert uow.index.get_version(V1, FAKE_IDENTITY.index_id) is None


def test_an_uncommitted_unit_of_work_stores_nothing(factory: RetrievalUnitOfWorkFactory) -> None:
    with factory.begin(system()) as uow:
        uow.index.add_version(version_record(), chunks(0x100, "a b", "c d"))
    with factory.begin(system()) as uow:
        assert uow.index.get_version(V1, FAKE_IDENTITY.index_id) is None


def test_nearest_is_ordered_paged_and_limited_to_the_given_documents(
    factory: RetrievalUnitOfWorkFactory,
) -> None:
    commit(factory, rows=chunks(0x100, "payment due monthly", "governing law new york"))
    commit(
        factory,
        record=version_record(document=D2, version=V2),
        rows=chunks(0x200, "payment due weekly", "payment penalties"),
    )
    q = vector("payment due monthly")
    with factory.begin(system()) as uow:
        both = uow.index.nearest(FAKE_IDENTITY.index_id, [D1, D2], q, 10, 0)
        only_d1 = uow.index.nearest(FAKE_IDENTITY.index_id, [D1], q, 10, 0)
        first = uow.index.nearest(FAKE_IDENTITY.index_id, [D1, D2], q, 2, 0)
        rest = uow.index.nearest(FAKE_IDENTITY.index_id, [D1, D2], q, 2, 2)
        beyond = uow.index.nearest(FAKE_IDENTITY.index_id, [D1, D2], q, 2, 4)
        none = uow.index.nearest(FAKE_IDENTITY.index_id, [], q, 10, 0)
        other_index = uow.index.nearest("0" * 32, [D1, D2], q, 10, 0)
    sims = [c.similarity for c in both]
    assert sims == sorted(sims, reverse=True) and len(both) == 4
    assert both[0].chunk_id.value.int == 0x100 and both[0].similarity == pytest.approx(1.0)
    assert {c.document_id for c in only_d1} == {D1}
    assert [c.chunk_id for c in first + rest] == [c.chunk_id for c in both]  # stable paging
    assert beyond == () and none == () and other_index == ()
    assert {(c.document_id, c.version_id) for c in both} == {(D1, V1), (D2, V2)}


def test_versions_of_lists_every_indexed_version_whatever_the_index(
    factory: RetrievalUnitOfWorkFactory,
) -> None:
    other = replace(FAKE_IDENTITY, version="2")
    commit(factory)
    commit(
        factory, record=version_record(version=V2, identity=other), rows=chunks(0x300, "a b", "c d")
    )
    with factory.begin(system()) as uow:
        found = uow.index.versions_of([D1, D2])
        assert found == {D1: tuple(sorted((V1, V2), key=str))}
        assert uow.index.list_for(D1) == tuple(sorted((V1, V2), key=str))
        assert uow.index.list_for(D2) == ()


def test_tenants_never_see_each_others_rows(factory: RetrievalUnitOfWorkFactory) -> None:
    commit(factory)
    with factory.begin(system(B)) as uow:
        assert uow.index.get_version(V1, FAKE_IDENTITY.index_id) is None
        assert uow.index.versions_of([D1]) == {}
        assert uow.index.nearest(FAKE_IDENTITY.index_id, [D1], vector("payment"), 10, 0) == ()
        assert uow.index.remove_all_for(D1) == 0
        uow.commit()
    with factory.begin(system(A)) as uow:  # tenant B's purge attempt changed nothing
        assert uow.index.get_version(V1, FAKE_IDENTITY.index_id) is not None


def test_remove_all_for_deletes_vectors_and_records_of_that_document_only(
    factory: RetrievalUnitOfWorkFactory,
) -> None:
    commit(factory)
    commit(
        factory, record=version_record(document=D2, version=V2), rows=chunks(0x400, "a b", "c d")
    )
    with factory.begin(system()) as uow:
        assert uow.index.remove_all_for(D1) == 1
        uow.commit()
    with factory.begin(system()) as uow:
        q = vector("payment")
        assert uow.index.nearest(FAKE_IDENTITY.index_id, [D1], q, 10, 0) == ()
        assert len(uow.index.nearest(FAKE_IDENTITY.index_id, [D2], q, 10, 0)) == 2


def test_receipts_are_insert_if_absent_and_tenant_bound(
    factory: RetrievalUnitOfWorkFactory,
) -> None:
    receipt = PurgeReceipt(A, D1, NOW, (V1, V2))
    with factory.begin(system()) as uow:
        assert uow.receipts.add(receipt)
        assert not uow.receipts.add(receipt)
        uow.commit()
    with factory.begin(system()) as uow:
        assert uow.receipts.get(D1) == receipt
        assert uow.receipts.get(D2) is None
    with factory.begin(system(B)) as uow:
        assert uow.receipts.get(D1) is None
