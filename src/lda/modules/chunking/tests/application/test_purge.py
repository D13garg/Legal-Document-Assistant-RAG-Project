"""PurgeChunks and its event handler: artifacts, rows, an insert-only receipt, retry safety."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from lda.modules.chunking.api import PURGE_SCOPE, PurgeChunksOnDocumentPurged, PurgeCommand
from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.record import PurgeReceipt
from lda.modules.chunking.tests.support import SYSTEM_A, TENANT_A, TENANT_B, Rig
from lda.modules.documents.api import DocumentId as SourceDocumentId
from lda.modules.documents.api import DocumentPurged
from lda.shared.kernel.errors import ConflictError, InfrastructureError, ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import EventId, TenantId

BODY = "# Title\n\nA clause worth purging."


def chunked(rig: Rig, tenant: object = TENANT_A) -> tuple[DocumentId, VersionId]:
    document, version = rig.upload_document(BODY, tenant=tenant)  # type: ignore[arg-type]
    rig.stage.run(rig.request(document, version, tenant=tenant))  # type: ignore[arg-type]
    assert rig.objects.exists(rig.key(document, version, tenant))  # type: ignore[arg-type]
    return document, version


def test_purge_removes_artifact_rows_and_writes_a_receipt(rig: Rig) -> None:
    document, version = chunked(rig)
    view = rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert (view.purged, view.versions_purged) == (True, 1)
    assert not rig.objects.exists(rig.key(document, version))
    assert not rig.store.chunk_sets
    receipt = rig.store.receipts[(TENANT_A, document)]
    assert (receipt.version_ids, receipt.scope) == ((version,), PURGE_SCOPE)
    assert receipt.purged_at == rig.clock.now()


def test_purge_leaves_other_documents_and_other_tenants_alone(rig: Rig) -> None:
    document, _ = chunked(rig)
    other_doc, other_ver = chunked(rig)
    foreign_doc, foreign_ver = chunked(rig, TENANT_B)
    rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert rig.objects.exists(rig.key(other_doc, other_ver))
    assert rig.objects.exists(rig.key(foreign_doc, foreign_ver, TENANT_B))
    assert len(rig.store.chunk_sets) == 2


def test_purge_covers_every_version_of_the_document(rig: Rig) -> None:
    document, first = chunked(rig)
    second = VersionId(UUID(int=0x5002))
    row = rig.store.chunk_sets[(TENANT_A, first)]
    rig.store.chunk_sets[(TENANT_A, second)] = replace(row, version_id=second)
    rig.objects.put(rig.key(document, second), b"{}", content_type="application/json")
    view = rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert view.versions_purged == 2
    assert not rig.objects.derived_keys() and not rig.store.chunk_sets


def test_second_purge_changes_nothing(rig: Rig) -> None:
    document, _ = chunked(rig)
    rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    receipt = rig.store.receipts[(TENANT_A, document)]
    again = rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert (again.purged, again.versions_purged) == (False, 0)
    assert rig.store.receipts[(TENANT_A, document)] == receipt


def test_a_document_without_chunks_still_gets_a_receipt(rig: Rig) -> None:
    document, _ = rig.upload_document(BODY, extract=False)
    assert rig.purge.execute(SYSTEM_A, PurgeCommand(document)).versions_purged == 0
    assert rig.store.receipts[(TENANT_A, document)].version_ids == ()


def test_storage_failure_keeps_rows_and_the_retry_completes(rig: Rig) -> None:
    document, _ = chunked(rig)
    rig.objects.fail_delete = True
    with pytest.raises(InfrastructureError):
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert rig.store.chunk_sets and (TENANT_A, document) not in rig.store.receipts
    rig.objects.fail_delete = False
    assert rig.purge.execute(SYSTEM_A, PurgeCommand(document)).purged


def test_a_delete_that_did_not_delete_is_detected(rig: Rig) -> None:
    document, _ = chunked(rig)
    rig.objects.delete = lambda key: None  # type: ignore[method-assign]
    with pytest.raises(InfrastructureError) as excinfo:
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert excinfo.value.code == "chunk_purge_verification_failed"
    assert rig.store.chunk_sets and not rig.store.receipts


def test_a_crash_before_the_final_commit_is_retried(rig: Rig) -> None:
    document, _ = chunked(rig)
    rig.store.fail_commit_on = {len(rig.chunking.begun) + 1}  # the purge's final transaction
    with pytest.raises(InfrastructureError):
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert rig.store.chunk_sets and not rig.store.receipts
    rig.store.fail_commit_on = set()
    assert rig.purge.execute(SYSTEM_A, PurgeCommand(document)).purged


def test_a_set_recorded_meanwhile_aborts_the_purge_and_the_retry_takes_it(rig: Rig) -> None:
    document, _ = chunked(rig)
    late_doc_version = VersionId(UUID(int=0x5003))
    original_delete = rig.objects.delete

    def record_late(key: object) -> None:
        original_delete(key)  # type: ignore[arg-type]
        row = next(iter(rig.store.chunk_sets.values()))
        rig.store.chunk_sets[(TENANT_A, late_doc_version)] = replace(
            row, version_id=late_doc_version
        )
        rig.objects.delete = original_delete  # type: ignore[method-assign]

    rig.objects.delete = record_late  # type: ignore[method-assign]
    with pytest.raises(ConflictError) as excinfo:
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert excinfo.value.code == "chunk_purge_version_set_changed"
    assert rig.purge.execute(SYSTEM_A, PurgeCommand(document)).versions_purged == 2


def test_the_final_transaction_removes_an_object_that_landed_after_the_first_pass(
    rig: Rig,
) -> None:
    document, version = chunked(rig)
    original_exists = rig.objects.exists
    calls = {"n": 0}

    def late_object(key: object) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:  # first verification passes, then a stale stage's put lands
            result = original_exists(key)  # type: ignore[arg-type]
            rig.objects._objects[rig.key(document, version).render()] = b"late"  # noqa: SLF001
            return result
        return original_exists(key)  # type: ignore[arg-type]

    rig.objects.exists = late_object  # type: ignore[method-assign]
    assert rig.purge.execute(SYSTEM_A, PurgeCommand(document)).purged
    assert not rig.objects.derived_keys()


# ---- the event handler --------------------------------------------------------------------------


def purged_event(rig: Rig, document: DocumentId, tenant: TenantId = TENANT_A) -> DocumentPurged:
    return DocumentPurged(
        event_id=EventId(UUID(int=0xE1)),
        tenant_id=tenant,
        occurred_at=rig.clock.now(),
        document_id=SourceDocumentId(document.value),
        versions_purged=1,
    )


def test_handler_purges_for_the_events_tenant_and_document(rig: Rig) -> None:
    document, version = chunked(rig)
    other, other_version = chunked(rig)
    handler = PurgeChunksOnDocumentPurged(rig.purge)
    handler.handle(purged_event(rig, document), rig.chunking.begin(SYSTEM_A))
    assert not rig.objects.exists(rig.key(document, version))
    assert (TENANT_A, document) in rig.store.receipts
    assert rig.objects.exists(rig.key(other, other_version))


def test_handler_acts_only_inside_the_events_own_tenant(rig: Rig) -> None:
    document, version = chunked(rig)
    handler = PurgeChunksOnDocumentPurged(rig.purge)
    handler.handle(purged_event(rig, document, TENANT_B), rig.chunking.begin(SYSTEM_A))
    assert rig.objects.exists(rig.key(document, version))
    assert (TENANT_A, version) in rig.store.chunk_sets
    assert (TENANT_A, document) not in rig.store.receipts


def test_handler_refuses_other_event_types_and_has_a_stable_name(rig: Rig) -> None:
    other = DomainEvent(
        event_id=EventId(UUID(int=1)), tenant_id=TENANT_A, occurred_at=rig.clock.now()
    )
    handler = PurgeChunksOnDocumentPurged(rig.purge)
    with pytest.raises(ValidationError):
        handler.handle(other, rig.chunking.begin(SYSTEM_A))
    assert handler.name == "chunking.purge_chunks"


def test_a_receipt_is_never_replaced(rig: Rig) -> None:
    document, _ = chunked(rig)
    rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    with rig.chunking.begin(SYSTEM_A) as uow:
        assert not uow.receipts.add(PurgeReceipt(TENANT_A, document, rig.clock.now(), ()))
