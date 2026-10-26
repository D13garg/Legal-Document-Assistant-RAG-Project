"""One behavioural contract, run against the in-memory and the SQL unit of work."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.domain.document import Document, DocumentVersion, VersionStatus
from lda.modules.documents.domain.evidence import PURGE_SCOPE, PurgeReceipt, UploadIntent
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.documents.tests.support import (
    ALICE,
    PDF,
    START,
    TENANT_A,
    TENANT_B,
    a_document_id,
    ctx_for,
)
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId

D1, D2 = a_document_id(1), a_document_id(2)
V1, V2, V3 = (DocumentVersionId(UUID(int=0x60 + n)) for n in (1, 2, 3))


@pytest.fixture(params=["memory", "sql"])
def factory(request: pytest.FixtureRequest) -> DocumentsUnitOfWorkFactory:
    name = "memory_factory" if request.param == "memory" else "sql_factory"
    value: DocumentsUnitOfWorkFactory = request.getfixturevalue(name)
    return value


def make(
    tenant: TenantId = TENANT_A,
    document_id: DocumentId = D1,
    version_id: DocumentVersionId = V1,
) -> tuple[Document, DocumentVersion]:
    return Document.create(
        document_id=document_id,
        version_id=version_id,
        tenant_id=tenant,
        title="Lease",
        media_type=PDF,
        size_bytes=12,
        created_by=ALICE,
        now=START,
    )


def seed(
    factory: DocumentsUnitOfWorkFactory,
    tenant: TenantId = TENANT_A,
    document_id: DocumentId = D1,
    version_id: DocumentVersionId = V1,
) -> None:
    document, version = make(tenant, document_id, version_id)
    with factory.begin(ctx_for(tenant)) as uow:
        uow.documents.add(document)
        uow.versions.add(version)
        uow.commit()


def tombstone(factory: DocumentsUnitOfWorkFactory, document_id: DocumentId = D1) -> None:
    with factory.begin(ctx_for(TENANT_A)) as uow:
        document = uow.documents.get(document_id, for_update=True)
        assert document is not None
        document.tombstone(START + timedelta(hours=1))
        uow.documents.save(document)
        uow.commit()


def test_a_document_and_its_version_round_trip_exactly(factory: DocumentsUnitOfWorkFactory) -> None:
    document, version = make()
    seed(factory)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.documents.get(D1) == document
        assert uow.documents.get(D1, for_update=True) == document
        assert uow.versions.get(V1) == version
        assert uow.versions.list_for(D1) == (version,)
        assert uow.documents.get(D2) is None and uow.versions.get(V2) is None


def test_system_authored_rows_round_trip_without_a_creator(
    factory: DocumentsUnitOfWorkFactory,
) -> None:
    document, version = make()
    document = replace(document, created_by=None)
    version = replace(version, created_by=None)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        uow.documents.add(document)
        uow.versions.add(version)
        uow.commit()
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.documents.get(D1) == document and uow.versions.get(V1) == version


def test_save_persists_only_the_mutable_fields(factory: DocumentsUnitOfWorkFactory) -> None:
    seed(factory)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        document = uow.documents.get(D1, for_update=True)
        assert document is not None
        second = document.add_version(
            version_id=V2, media_type=PDF, size_bytes=3, created_by=ALICE, now=START
        )
        uow.documents.save(document)
        uow.versions.add(second)
        uow.commit()
    with factory.begin(ctx_for(TENANT_A)) as uow:
        document = uow.documents.get(D1)
        assert document is not None and document.latest_version_number == 2
        assert [v.number for v in uow.versions.list_for(D1)] == [1, 2]


def test_saving_an_unknown_document_fails(factory: DocumentsUnitOfWorkFactory) -> None:
    document, _ = make(document_id=D2)
    with factory.begin(ctx_for(TENANT_A)) as uow, pytest.raises(InfrastructureError) as caught:
        uow.documents.save(document)
    assert caught.value.code == "document_not_saved"


def test_a_tombstone_is_irreversible_and_immutable(factory: DocumentsUnitOfWorkFactory) -> None:
    seed(factory)
    tombstone(factory)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        document = uow.documents.get(D1)
        assert document is not None and document.deleted_at is not None
        revived = replace(document, status=type(document.status).ACTIVE, deleted_at=None)
        with pytest.raises(InfrastructureError):
            uow.documents.save(revived)


def test_only_a_tombstoned_document_can_be_removed_and_versions_go_first(
    factory: DocumentsUnitOfWorkFactory,
) -> None:
    seed(factory)
    with factory.begin(ctx_for(TENANT_A)) as uow, pytest.raises(InfrastructureError):
        uow.versions.remove_all_for(D1)  # not tombstoned
    with factory.begin(ctx_for(TENANT_A)) as uow, pytest.raises(InfrastructureError):
        uow.documents.remove(D1)
    tombstone(factory)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.versions.remove_all_for(D1) == 1
        assert uow.documents.remove(D1) is True
        assert uow.documents.remove(D1) is False
        uow.commit()
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.documents.get(D1) is None and uow.versions.list_for(D1) == ()


def test_a_version_needs_its_document_and_a_unique_number(
    factory: DocumentsUnitOfWorkFactory,
) -> None:
    _, orphan = make(document_id=D2, version_id=V3)
    with factory.begin(ctx_for(TENANT_A)) as uow, pytest.raises(InfrastructureError):
        uow.versions.add(orphan)
    seed(factory)
    _, clash = make(version_id=V2)  # number 1 again for the same document
    with factory.begin(ctx_for(TENANT_A)) as uow, pytest.raises(InfrastructureError):
        uow.versions.add(clash)


def test_upload_intents_add_get_remove(factory: DocumentsUnitOfWorkFactory) -> None:
    intent = UploadIntent(TENANT_A, V1, D1, START)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.intents.get(V1) is None
        uow.intents.add(intent)
        uow.commit()
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.intents.get(V1) == intent
        assert uow.intents.get(V1, for_update=True) == intent
        with pytest.raises(InfrastructureError):
            uow.intents.add(intent)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.intents.remove(V1) is True
        assert uow.intents.remove(V1) is False
        uow.commit()
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.intents.get(V1) is None


def test_purge_receipts_round_trip_and_are_unique(factory: DocumentsUnitOfWorkFactory) -> None:
    receipt = PurgeReceipt(TENANT_A, D1, START, START + timedelta(hours=2), (V1, V2))
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.receipts.get(D1) is None
        uow.receipts.add(receipt)
        uow.commit()
    with factory.begin(ctx_for(TENANT_A)) as uow:
        found = uow.receipts.get(D1)
        assert found == receipt and found.versions_purged == 2 and found.scope == PURGE_SCOPE
        with pytest.raises(InfrastructureError):
            uow.receipts.add(receipt)


def test_tenants_never_see_each_other(factory: DocumentsUnitOfWorkFactory) -> None:
    seed(factory)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        uow.intents.add(UploadIntent(TENANT_A, V2, D1, START))
        uow.receipts.add(PurgeReceipt(TENANT_A, D2, START, START, ()))
        uow.commit()
    with factory.begin(ctx_for(TENANT_B)) as uow:
        assert uow.documents.get(D1) is None
        assert uow.versions.get(V1) is None and uow.versions.list_for(D1) == ()
        assert uow.intents.get(V2) is None and uow.receipts.get(D2) is None
        assert uow.versions.remove_all_for(D1) == 0
        assert uow.documents.remove(D1) is False
        assert uow.intents.remove(V2) is False


def test_a_bound_repository_refuses_another_tenants_rows(
    factory: DocumentsUnitOfWorkFactory,
) -> None:
    document, version = make(tenant=TENANT_B)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        actions: tuple[Callable[[], None], ...] = (
            lambda: uow.documents.add(document),
            lambda: uow.versions.add(version),
            lambda: uow.intents.add(UploadIntent(TENANT_B, V1, D1, START)),
            lambda: uow.receipts.add(PurgeReceipt(TENANT_B, D1, START, START, ())),
        )
        for action in actions:
            with pytest.raises(InfrastructureError) as caught:
                action()
            assert caught.value.code == "documents_tenant_mismatch"


def test_uncommitted_work_is_rolled_back_but_visible_inside_the_unit(
    factory: DocumentsUnitOfWorkFactory,
) -> None:
    document, version = make()
    with factory.begin(ctx_for(TENANT_A)) as uow:
        uow.documents.add(document)
        uow.versions.add(version)
        assert uow.documents.get(D1) == document
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.documents.get(D1) is None and uow.versions.list_for(D1) == ()


# ---- Step 6: terminal outcomes (ADR-015 amendment) ---------------------------------------------


def current(factory: DocumentsUnitOfWorkFactory, version_id: DocumentVersionId = V1) -> str:
    with factory.begin(ctx_for(TENANT_A)) as uow:
        version = uow.versions.get(version_id)
        assert version is not None
        return version.status.value


@pytest.mark.parametrize("outcome", ["ready", "failed", "quarantined"])
def test_set_outcome_moves_a_pending_version_exactly_once(
    factory: DocumentsUnitOfWorkFactory, outcome: str
) -> None:
    seed(factory)
    status = VersionStatus(outcome)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.versions.set_outcome(V1, status) is True
        uow.commit()
    assert current(factory) == outcome
    for other in VersionStatus:  # terminal: nothing moves it again, not even to the same value
        with factory.begin(ctx_for(TENANT_A)) as uow:
            assert uow.versions.set_outcome(V1, other) is False
            uow.commit()
    assert current(factory) == outcome


def test_set_outcome_of_an_unknown_version_or_another_tenants_version_changes_nothing(
    factory: DocumentsUnitOfWorkFactory,
) -> None:
    seed(factory)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        assert uow.versions.set_outcome(V2, VersionStatus.READY) is False
    with factory.begin(ctx_for(TENANT_B)) as uow:
        assert uow.versions.set_outcome(V1, VersionStatus.READY) is False
        uow.commit()
    assert current(factory) == "pending"


def test_set_outcome_persists_only_the_status_and_leaves_the_rest_of_the_version_alone(
    factory: DocumentsUnitOfWorkFactory,
) -> None:
    _, before = make()
    seed(factory)
    with factory.begin(ctx_for(TENANT_A)) as uow:
        uow.versions.set_outcome(V1, VersionStatus.READY)
        uow.commit()
    with factory.begin(ctx_for(TENANT_A)) as uow:
        after = uow.versions.get(V1)
    assert after == replace(before, status=VersionStatus.READY)
