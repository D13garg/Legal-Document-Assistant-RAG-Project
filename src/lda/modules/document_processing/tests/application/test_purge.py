"""PurgeExtractions and its event handler: artifacts, rows and an insert-only receipt."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from lda.modules.document_processing.api import (
    PURGE_SCOPE,
    PurgeCommand,
    PurgeExtractionsOnDocumentPurged,
)
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.tests.support import (
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    Rig,
)
from lda.modules.documents.api import DocumentId as SourceDocumentId
from lda.modules.documents.api import DocumentPurged
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError, ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import EventId

BODY = b"A clause worth purging."


def extracted(rig: Rig, tenant: object = TENANT_A) -> tuple[DocumentId, VersionId]:
    document, version = rig.upload_document(BODY, tenant=tenant)  # type: ignore[arg-type]
    outcome = rig.stage.run(rig.request(document, version, tenant=tenant))  # type: ignore[arg-type]
    assert type(outcome).__name__ == "StageSucceeded"
    return document, version


def test_purge_removes_artifact_rows_and_writes_a_receipt(rig: Rig) -> None:
    document, version = extracted(rig)
    view = rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert (view.purged, view.versions_purged) == (True, 1)
    assert not rig.objects.exists(rig.key(document, version))
    assert not rig.store.extractions
    receipt = rig.store.receipts[(TENANT_A, document)]
    assert receipt.version_ids == (version,)
    assert receipt.scope == PURGE_SCOPE
    assert receipt.purged_at == rig.clock.now()


def test_purge_covers_every_version_of_the_document(rig: Rig) -> None:
    document, first = extracted(rig)
    second = VersionId(UUID(int=0x5002))
    rig.upload_document(b"unrelated")  # a different document: must survive
    other_document, other_version = extracted(rig)
    # a second version of the same document, recorded the way the stage records it
    row = rig.store.extractions[(TENANT_A, first)]
    rig.store.extractions[(TENANT_A, second)] = replace(row, version_id=second)
    rig.objects.put(rig.key(document, second), b"{}", content_type="application/json")
    view = rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert view.versions_purged == 2
    assert set(rig.store.receipts[(TENANT_A, document)].version_ids) == {first, second}
    assert rig.objects.exists(rig.key(other_document, other_version))
    assert (TENANT_A, other_version) in rig.store.extractions


def test_purge_is_tenant_scoped(rig: Rig) -> None:
    document, version = extracted(rig)
    other = SystemContext(TENANT_B, "document-processing-test")
    view = rig.purge.execute(other, PurgeCommand(document))
    assert view.versions_purged == 0  # nothing of tenant B's under that document id
    assert rig.objects.exists(rig.key(document, version))
    assert (TENANT_A, version) in rig.store.extractions
    assert (TENANT_A, document) not in rig.store.receipts


def test_second_purge_changes_nothing(rig: Rig) -> None:
    document, _ = extracted(rig)
    rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    before = dict(rig.store.receipts)
    view = rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert (view.purged, view.versions_purged) == (False, 0)
    assert rig.store.receipts == before


def test_document_without_extractions_still_gets_a_receipt(rig: Rig) -> None:
    document = DocumentId(UUID(int=0xC0DE))
    view = rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert (view.purged, view.versions_purged) == (True, 0)
    assert rig.store.receipts[(TENANT_A, document)].version_ids == ()


def test_storage_failure_keeps_rows_and_retry_completes(rig: Rig) -> None:
    document, version = extracted(rig)
    rig.objects.fail_delete = True
    with pytest.raises(InfrastructureError):
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert (TENANT_A, version) in rig.store.extractions  # still findable for the retry
    assert (TENANT_A, document) not in rig.store.receipts
    rig.objects.fail_delete = False
    assert rig.purge.execute(SYSTEM_A, PurgeCommand(document)).purged is True
    assert not rig.objects.exists(rig.key(document, version))


def test_a_delete_that_did_not_delete_is_detected(rig: Rig) -> None:
    document, version = extracted(rig)
    rig.objects.ignore_delete = True
    with pytest.raises(InfrastructureError) as excinfo:
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert excinfo.value.code == "extraction_purge_verification_failed"
    assert (TENANT_A, version) in rig.store.extractions
    assert (TENANT_A, document) not in rig.store.receipts


def test_crash_before_the_final_commit_is_retried(rig: Rig) -> None:
    document, _ = extracted(rig)
    rig.store.fail_commit_on = {len(rig.processing.begun) + 1}  # the purge's write transaction
    with pytest.raises(InfrastructureError):
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert rig.store.extractions  # rows survive; artifacts are already gone: that is fine
    rig.store.fail_commit_on = set()
    assert rig.purge.execute(SYSTEM_A, PurgeCommand(document)).purged is True
    assert not rig.store.extractions


def test_a_version_recorded_meanwhile_aborts_the_purge(rig: Rig) -> None:
    document, first = extracted(rig)
    late = VersionId(UUID(int=0x5003))

    def a_stage_records_late() -> None:
        row = rig.store.extractions[(TENANT_A, first)]
        rig.store.extractions[(TENANT_A, late)] = replace(row, version_id=late)

    rig.objects.after_delete = a_stage_records_late
    with pytest.raises(ConflictError) as excinfo:
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert excinfo.value.code == "extraction_purge_version_set_changed"
    assert (TENANT_A, document) not in rig.store.receipts
    rig.objects.after_delete = None
    assert rig.purge.execute(SYSTEM_A, PurgeCommand(document)).versions_purged == 2


# ---- the event handler -------------------------------------------------------------------------


def purged_event(rig: Rig, document: DocumentId, tenant: object = TENANT_A) -> DocumentPurged:
    return DocumentPurged(
        event_id=EventId(UUID(int=0xE1)),
        tenant_id=tenant,  # type: ignore[arg-type]
        occurred_at=rig.clock.now(),
        document_id=SourceDocumentId(document.value),
        versions_purged=1,
    )


def test_handler_purges_for_the_events_tenant_and_document(rig: Rig) -> None:
    document, version = extracted(rig)
    handler = PurgeExtractionsOnDocumentPurged(rig.purge)
    handler.handle(purged_event(rig, document), rig.processing.begin(SYSTEM_A))
    assert not rig.objects.exists(rig.key(document, version))
    assert (TENANT_A, document) in rig.store.receipts


def test_handler_ignores_other_tenants_data(rig: Rig) -> None:
    document, version = extracted(rig)
    handler = PurgeExtractionsOnDocumentPurged(rig.purge)
    handler.handle(purged_event(rig, document, TENANT_B), rig.processing.begin(SYSTEM_A))
    assert rig.objects.exists(rig.key(document, version))
    assert (TENANT_A, version) in rig.store.extractions


def test_handler_refuses_other_event_types(rig: Rig) -> None:
    handler = PurgeExtractionsOnDocumentPurged(rig.purge)
    other = DomainEvent(
        event_id=EventId(UUID(int=1)), tenant_id=TENANT_A, occurred_at=rig.clock.now()
    )
    with pytest.raises(ValidationError):
        handler.handle(other, rig.processing.begin(SYSTEM_A))


def test_handler_name_is_stable() -> None:
    assert PurgeExtractionsOnDocumentPurged.name == "document_processing.purge_extractions"
