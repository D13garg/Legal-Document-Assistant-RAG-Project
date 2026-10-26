"""PurgeRetrievalIndex and its handler: rows, an insert-only receipt, retry and race safety."""

from __future__ import annotations

from uuid import UUID

import pytest

from lda.modules.documents.api import DocumentId as SourceDocumentId
from lda.modules.documents.api import DocumentPurged
from lda.modules.ingestion.api import StagePermanentFailure, StageSucceeded
from lda.modules.retrieval.api import PURGE_SCOPE, PurgeRetrievalIndexOnDocumentPurged
from lda.modules.retrieval.domain.failures import VERSION_UNAVAILABLE
from lda.modules.retrieval.domain.ids import DocumentId, VersionId
from lda.modules.retrieval.tests.support import SYSTEM_A, TENANT_A, TENANT_B, Rig
from lda.shared.kernel.errors import InfrastructureError, ValidationError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import EventId, TenantId

TEXTS = ["Payment is due monthly", "Late payment accrues interest"]


def _indexed(rig: Rig) -> tuple[DocumentId, VersionId]:
    document, version = rig.new_document(TEXTS)
    assert isinstance(rig.index(document, version), StageSucceeded)
    rig.ready(document, version)
    return document, version


def test_the_purge_removes_every_row_and_writes_a_scoped_receipt(rig: Rig) -> None:
    document, _ = _indexed(rig)
    v2 = rig.new_version(document, TEXTS)
    rig.index(document, v2)
    view = rig.purge_document(document)
    store = rig.retrieval.store
    assert view.purged and view.versions_purged == 2
    assert not store.rows and not store.versions
    receipt = store.receipts[(TENANT_A, document)]
    assert receipt.scope == PURGE_SCOPE and len(receipt.version_ids) == 2


def test_a_purge_with_nothing_indexed_still_writes_its_receipt(rig: Rig) -> None:
    document, _ = rig.new_document(TEXTS)
    view = rig.purge_document(document)
    assert view.purged and view.versions_purged == 0
    assert (TENANT_A, document) in rig.retrieval.store.receipts


def test_the_purge_is_idempotent_and_never_replaces_its_receipt(rig: Rig) -> None:
    document, _ = _indexed(rig)
    first = rig.purge_document(document)
    receipt = rig.retrieval.store.receipts[(TENANT_A, document)]
    again = rig.purge_document(document)
    assert first.purged and not again.purged and again.versions_purged == 0
    assert rig.retrieval.store.receipts[(TENANT_A, document)] is receipt


def test_a_failed_purge_changes_nothing_and_a_retry_completes_it(rig: Rig) -> None:
    document, _ = _indexed(rig)
    store = rig.retrieval.store
    store.fail_commit_on = {len(rig.retrieval.begun)}
    with pytest.raises(InfrastructureError):
        rig.purge_document(document)
    assert store.rows and store.versions and not store.receipts  # all or nothing
    store.fail_commit_on = set()
    assert rig.purge_document(document).purged and not store.rows


def test_the_purge_takes_the_lifecycle_lock(rig: Rig) -> None:
    document, _ = _indexed(rig)
    before = len(rig.retrieval.begun)
    rig.purge_document(document)
    assert document in rig.retrieval.begun[before].locked


def test_only_the_purged_document_is_touched(rig: Rig) -> None:
    doomed, _ = _indexed(rig)
    kept, _ = _indexed(rig)
    rig.purge_document(doomed)
    assert {r.document_id for r in rig.retrieval.store.rows.values()} == {kept}
    assert rig.search("payment", [kept]).hits


def test_search_finds_nothing_after_the_purge_and_a_late_stage_is_refused(rig: Rig) -> None:
    document, version = _indexed(rig)
    rig.purge_document(document)
    assert rig.search("payment", [document]).hits == ()
    # a redelivered or delayed stage after the purge must not resurrect anything
    assert rig.index(document, version) == StagePermanentFailure(VERSION_UNAVAILABLE)
    assert not rig.retrieval.store.rows


def _event(rig: Rig, document: DocumentId, tenant: TenantId = TENANT_A) -> DocumentPurged:
    return DocumentPurged(
        event_id=EventId(UUID(int=0xE1)),
        tenant_id=tenant,
        occurred_at=rig.clock.now(),
        document_id=SourceDocumentId(document.value),
        versions_purged=1,
    )


def test_the_handler_purges_for_the_events_tenant_and_document(rig: Rig) -> None:
    document, _ = _indexed(rig)
    handler = PurgeRetrievalIndexOnDocumentPurged(rig.purge)
    handler.handle(_event(rig, document), rig.retrieval.begin(SYSTEM_A))
    handler.handle(_event(rig, document), rig.retrieval.begin(SYSTEM_A))  # duplicate delivery
    assert not rig.retrieval.store.rows and (TENANT_A, document) in rig.retrieval.store.receipts


def test_the_handler_acts_only_inside_the_events_own_tenant(rig: Rig) -> None:
    document, _ = _indexed(rig)
    handler = PurgeRetrievalIndexOnDocumentPurged(rig.purge)
    handler.handle(_event(rig, document, TENANT_B), rig.retrieval.begin(SYSTEM_A))
    assert rig.retrieval.store.rows  # tenant A's rows survive an event of tenant B
    assert (TENANT_A, document) not in rig.retrieval.store.receipts


def test_the_handler_refuses_other_event_types_and_has_a_stable_name(rig: Rig) -> None:
    other = DomainEvent(
        event_id=EventId(UUID(int=1)), tenant_id=TENANT_A, occurred_at=rig.clock.now()
    )
    handler = PurgeRetrievalIndexOnDocumentPurged(rig.purge)
    with pytest.raises(ValidationError):
        handler.handle(other, rig.retrieval.begin(SYSTEM_A))
    assert handler.name == "retrieval.purge_index"
