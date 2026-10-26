"""One behavioural contract, run against the in-memory and the SQL unit of work."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from lda.modules.document_processing.application.ports import ProcessingUnitOfWorkFactory
from lda.modules.document_processing.domain.extraction import Block, BlockKind, ExtractedDocument
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.domain.record import ExtractionRecord, PurgeReceipt
from lda.modules.document_processing.tests.support import ctx
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
    pages: int | None = None,
) -> ExtractionRecord:
    block = Block(BlockKind.PARAGRAPH, text, page=1 if pages else None)
    extracted = ExtractedDocument("application/pdf", (block,), pages)
    return ExtractionRecord.describe(
        tenant_id=tenant,
        document_id=document,
        version_id=version,
        parser_version="extract-1; pypdf=1; defusedxml=1",
        extracted=extracted,
        now=NOW,
    )[0]


def commit(factory: ProcessingUnitOfWorkFactory, context: ExecutionContext, *rows: object) -> None:
    with factory.begin(context) as uow:
        for row in rows:
            if isinstance(row, ExtractionRecord):
                assert uow.extractions.add(row)
            else:
                assert isinstance(row, PurgeReceipt)
                assert uow.receipts.add(row)
        uow.commit()


def test_record_round_trips_exactly(factory: ProcessingUnitOfWorkFactory) -> None:
    stored = record(pages=3)
    commit(factory, ctx(A), stored)
    with factory.begin(ctx(A)) as uow:
        assert uow.extractions.get(V1) == stored
        assert uow.extractions.get(V2) is None


def test_add_is_insert_if_absent_and_never_replaces(factory: ProcessingUnitOfWorkFactory) -> None:
    first = record(text="first")
    commit(factory, ctx(A), first)
    with factory.begin(ctx(A)) as uow:
        assert uow.extractions.add(record(text="second")) is False
        uow.commit()
    with factory.begin(ctx(A)) as uow:
        assert uow.extractions.get(V1) == first


def test_uncommitted_work_is_discarded(factory: ProcessingUnitOfWorkFactory) -> None:
    with factory.begin(ctx(A)) as uow:
        assert uow.extractions.add(record())
    with factory.begin(ctx(A)) as uow:
        assert uow.extractions.get(V1) is None


def test_exception_inside_the_block_discards_work(factory: ProcessingUnitOfWorkFactory) -> None:
    with pytest.raises(RuntimeError), factory.begin(ctx(A)) as uow:
        uow.extractions.add(record())
        raise RuntimeError("boom")
    with factory.begin(ctx(A)) as uow:
        assert uow.extractions.get(V1) is None


def test_list_is_scoped_to_document_and_ordered(factory: ProcessingUnitOfWorkFactory) -> None:
    commit(
        factory,
        ctx(A),
        record(version=V2),
        record(version=V1),
        record(document=D2, version=V3),
    )
    with factory.begin(ctx(A)) as uow:
        assert [r.version_id for r in uow.extractions.list_for(D1)] == [V1, V2]
        assert [r.version_id for r in uow.extractions.list_for(D2)] == [V3]
        assert uow.extractions.list_for(DocumentId(UUID(int=0xFF))) == ()


def test_tenants_never_see_each_other(factory: ProcessingUnitOfWorkFactory) -> None:
    commit(factory, ctx(A), record())
    with factory.begin(ctx(B)) as uow:
        assert uow.extractions.get(V1) is None
        assert uow.extractions.list_for(D1) == ()
        assert uow.extractions.remove_all_for(D1) == 0
        # the same version id in another tenant is a different row
        assert uow.extractions.add(record(tenant=B)) is True
        uow.commit()
    with factory.begin(ctx(A)) as uow:
        assert uow.extractions.get(V1) is not None


def test_a_record_of_another_tenant_cannot_be_written(factory: ProcessingUnitOfWorkFactory) -> None:
    with factory.begin(ctx(A)) as uow, pytest.raises(InfrastructureError):
        uow.extractions.add(record(tenant=B))
    with factory.begin(ctx(A)) as uow, pytest.raises(InfrastructureError):
        uow.receipts.add(PurgeReceipt(B, D1, NOW, ()))


def test_remove_all_only_removes_the_documents_rows(factory: ProcessingUnitOfWorkFactory) -> None:
    commit(factory, ctx(A), record(version=V1), record(version=V2), record(document=D2, version=V3))
    with factory.begin(ctx(A)) as uow:
        assert uow.extractions.remove_all_for(D1) == 2
        uow.commit()
    with factory.begin(ctx(A)) as uow:
        assert uow.extractions.list_for(D1) == ()
        assert uow.extractions.get(V3) is not None


def test_receipt_round_trips_and_is_insert_if_absent(factory: ProcessingUnitOfWorkFactory) -> None:
    receipt = PurgeReceipt(A, D1, NOW, (V1, V2))
    commit(factory, ctx(A), receipt)
    with factory.begin(ctx(A)) as uow:
        assert uow.receipts.get(D1) == receipt
        assert uow.receipts.add(replace(receipt, version_ids=())) is False
        assert uow.receipts.get(D1) == receipt
        assert uow.receipts.get(D2) is None
    with factory.begin(ctx(B)) as uow:
        assert uow.receipts.get(D1) is None


def test_receipt_of_a_document_without_versions(factory: ProcessingUnitOfWorkFactory) -> None:
    commit(factory, ctx(A), PurgeReceipt(A, D2, NOW, ()))
    with factory.begin(ctx(A)) as uow:
        stored = uow.receipts.get(D2)
        assert stored is not None and stored.versions_purged == 0
