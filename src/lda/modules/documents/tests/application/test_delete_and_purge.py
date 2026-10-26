"""Tombstone + revocation are one transaction; purge is idempotent and proves only its own scope."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.documents.application.dto.documents import (
    AddDocumentVersionCommand,
    DocumentReference,
)
from lda.modules.documents.application.jobs import DOCUMENTS_QUEUE, PURGE_JOB
from lda.modules.documents.domain.document import DocumentStatus
from lda.modules.documents.domain.events import DocumentDeleted, DocumentPurged
from lda.modules.documents.domain.evidence import PURGE_SCOPE, PurgeReceipt
from lda.modules.documents.domain.resource import original_path
from lda.modules.documents.testing import InMemoryDocumentsStore, InMemoryDocumentsUnitOfWorkFactory
from lda.modules.documents.tests.support import (
    ALICE,
    PDF,
    START,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    FlakyStore,
    Rig,
    build_rig,
    ctx_for,
    uploaded_ids,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConflictError, InfrastructureError, NotFoundError
from lda.shared.ports.audit import AuditOutcome
from lda.shared.ports.events import Delivery
from lda.shared.ports.object_store import TenantScopedKey

CTX = ctx_for(TENANT_A, ALICE, 5)


def grants_key(document_id) -> tuple:  # type: ignore[no-untyped-def,type-arg]
    return (TENANT_A, "document", str(document_id))


def key(document_id, version_id) -> TenantScopedKey:  # type: ignore[no-untyped-def]
    return TenantScopedKey(TENANT_A, original_path(document_id, version_id))


def two_versions(rig: Rig):  # type: ignore[no-untyped-def]
    document_id, v1 = uploaded_ids(rig, CTX)
    v2 = rig.add_version.execute(
        CTX, AddDocumentVersionCommand(document_id, PDF, b"second")
    ).version_id
    return document_id, v1, v2


# ---- delete ------------------------------------------------------------------------------------


def test_delete_tombstones_revokes_enqueues_publishes_and_audits_in_one_unit(rig: Rig) -> None:
    store: InMemoryDocumentsStore = rig.factory.store
    document_id, _ = uploaded_ids(rig, CTX)
    store.grants[grants_key(document_id)] = 3
    before = len(rig.factory.begun)
    rig.clock.advance(timedelta(minutes=5))
    view = rig.delete.execute(CTX, DocumentReference(document_id))
    (uow,) = rig.factory.begun[before:]  # exactly ONE transaction
    assert uow.committed
    assert (view.changed, view.revoked_grants) == (True, 3)
    document = store.documents[(TENANT_A, document_id)]
    assert (document.status, document.deleted_at) == (DocumentStatus.DELETED, rig.clock.now())
    assert store.grants[grants_key(document_id)] == 0
    assert store.revocations == [(grants_key(document_id), 3)]
    (spec,) = uow.jobs.jobs
    assert (spec.queue, spec.job_type, spec.payload) == (
        DOCUMENTS_QUEUE,
        PURGE_JOB,
        {"document_id": str(document_id)},
    )
    assert spec.unique_key == f"{PURGE_JOB}:{document_id}" and spec.run_at is None
    (event, delivery) = uow.events.events[0]
    assert isinstance(event, DocumentDeleted) and delivery is Delivery.ASYNC_JOB
    (fact,) = uow.audit.facts
    assert (fact.action, fact.resource_id, fact.actor, fact.outcome) == (
        "documents.document_deleted",
        str(document_id),
        ALICE,
        AuditOutcome.SUCCESS,
    )
    assert dict(fact.attributes) == {"revoked_grants": "3"}


def test_a_failing_revocation_aborts_the_whole_delete(rig: Rig) -> None:
    store: InMemoryDocumentsStore = rig.factory.store
    document_id, _ = uploaded_ids(rig, CTX)
    store.grants[grants_key(document_id)] = 2
    store.fail_revocation = True
    with pytest.raises(InfrastructureError):
        rig.delete.execute(CTX, DocumentReference(document_id))
    assert store.documents[(TENANT_A, document_id)].status is DocumentStatus.ACTIVE
    assert store.grants[grants_key(document_id)] == 2 and store.revocations == []
    last = rig.factory.begun[-1]
    assert not last.committed and last.rolled_back


def test_a_failure_after_the_revocation_restores_the_grants_too(
    memory_store: InMemoryDocumentsStore, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    from lda.modules.documents.tests.support import FaultyFactory

    inner = InMemoryDocumentsUnitOfWorkFactory(memory_store)
    rig = build_rig(inner, clock, ids)
    document_id, _ = uploaded_ids(rig, CTX)
    memory_store.grants[grants_key(document_id)] = 4
    faulty = build_rig(FaultyFactory(inner, fail_commit_on=len(inner.begun)), clock, ids)
    with pytest.raises(InfrastructureError):
        faulty.delete.execute(CTX, DocumentReference(document_id))
    assert memory_store.documents[(TENANT_A, document_id)].status is DocumentStatus.ACTIVE
    assert memory_store.grants[grants_key(document_id)] == 4  # no document without its access


def test_deleting_twice_is_a_silent_no_op(rig: Rig) -> None:
    document_id, _ = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    store: InMemoryDocumentsStore = rig.factory.store
    before = (len(store.revocations), sum(len(u.audit.facts) for u in rig.factory.begun))
    view = rig.delete.execute(CTX, DocumentReference(document_id))
    assert (view.changed, view.revoked_grants) == (False, 0)
    after = (len(store.revocations), sum(len(u.audit.facts) for u in rig.factory.begun))
    assert after == before
    assert (
        sum(
            len(u.jobs.jobs)
            for u in rig.factory.begun
            if u.jobs.jobs and u.jobs.jobs[0].job_type == PURGE_JOB
        )
        == 1
    )


def test_deleting_an_unknown_or_foreign_document_is_not_found_and_changes_nothing(rig: Rig) -> None:
    document_id, _ = uploaded_ids(rig, CTX)
    rig.factory.store.grants[grants_key(document_id)] = 1
    with pytest.raises(NotFoundError):
        rig.delete.execute(ctx_for(TENANT_B), DocumentReference(document_id))
    assert rig.factory.store.grants[grants_key(document_id)] == 1


def test_a_tombstoned_document_is_unreadable_and_takes_no_new_version(rig: Rig) -> None:
    document_id, _ = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    with pytest.raises(NotFoundError):
        rig.get.execute(CTX, DocumentReference(document_id))
    with pytest.raises(ConflictError):
        rig.add_version.execute(CTX, AddDocumentVersionCommand(document_id, PDF, b"x"))


# ---- purge -------------------------------------------------------------------------------------


def test_purge_removes_objects_and_rows_and_writes_a_receipt_that_claims_only_that(
    rig: Rig,
) -> None:
    store: InMemoryDocumentsStore = rig.factory.store
    document_id, v1, v2 = two_versions(rig)
    rig.delete.execute(CTX, DocumentReference(document_id))
    tombstoned_at = store.documents[(TENANT_A, document_id)].deleted_at
    assert tombstoned_at is not None
    rig.clock.advance(timedelta(hours=1))
    view = rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert (view.purged, view.versions_purged) == (True, 2)
    assert not rig.store.exists(key(document_id, v1)) and not rig.store.exists(key(document_id, v2))
    assert store.documents == {} and store.versions == {}
    receipt = store.receipts[(TENANT_A, document_id)]
    assert receipt == PurgeReceipt(TENANT_A, document_id, tombstoned_at, rig.clock.now(), (v1, v2))
    assert receipt.scope == PURGE_SCOPE and receipt.versions_purged == 2
    # the receipt has no field that could imply other stores were purged
    assert {f.name for f in dataclasses.fields(receipt)} == {
        "tenant_id",
        "document_id",
        "tombstoned_at",
        "purged_at",
        "version_ids",
        "scope",
    }
    assert PURGE_SCOPE == "documents.originals_and_rows"


def test_purge_audits_and_publishes_identifier_only_facts(rig: Rig) -> None:
    document_id, _, _ = two_versions(rig)
    rig.delete.execute(CTX, DocumentReference(document_id))
    rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    uow = rig.factory.begun[-1]
    (fact,) = uow.audit.facts
    assert (fact.action, fact.resource_id, fact.actor) == (
        "documents.document_purged",
        str(document_id),
        None,
    )
    assert dict(fact.attributes) == {"purpose": "documents-test", "versions_purged": "2"}
    (event, delivery) = uow.events.events[0]
    assert isinstance(event, DocumentPurged) and delivery is Delivery.ASYNC_JOB
    assert event.versions_purged == 2


def test_purge_is_idempotent(rig: Rig) -> None:
    document_id, _ = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    first = rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    facts_before = sum(len(u.audit.facts) for u in rig.factory.begun)
    second = rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert (first.purged, second.purged, second.versions_purged) == (True, False, 0)
    assert sum(len(u.audit.facts) for u in rig.factory.begun) == facts_before


def test_purge_resumes_after_a_crash_between_object_deletion_and_the_commit(rig: Rig) -> None:
    document_id, v1 = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    rig.store.delete(key(document_id, v1))  # the previous attempt got this far, then died
    view = rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert view.purged and (TENANT_A, document_id) in rig.factory.store.receipts


def test_purge_refuses_a_document_that_is_not_tombstoned_and_deletes_nothing(rig: Rig) -> None:
    document_id, v1 = uploaded_ids(rig, CTX)
    with pytest.raises(ConflictError) as caught:
        rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert caught.value.code == "document_not_deleted"
    assert rig.store.exists(key(document_id, v1))


def test_purge_of_an_unknown_or_foreign_document_does_nothing(rig: Rig) -> None:
    document_id, v1 = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    foreign = rig.purge.execute(type(SYSTEM_A)(TENANT_B, "x"), DocumentReference(document_id))
    assert foreign.purged is False and rig.store.exists(key(document_id, v1))


def test_a_failing_object_delete_keeps_every_row_and_writes_no_receipt(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    document_id, _ = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    store.fail_delete = True
    with pytest.raises(InfrastructureError):
        rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert memory_factory.store.receipts == {} and memory_factory.store.documents
    store.fail_delete = False
    assert rig.purge.execute(SYSTEM_A, DocumentReference(document_id)).purged  # a retry completes


def test_an_unverifiable_delete_is_never_reported_as_purged(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    document_id, _ = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    store.ignore_delete = True  # the store claims success but the object is still there
    with pytest.raises(InfrastructureError) as caught:
        rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert caught.value.code == "purge_verification_failed"
    assert memory_factory.store.receipts == {} and memory_factory.store.documents


def test_a_failure_in_the_final_transaction_leaves_rows_and_no_receipt_and_the_retry_completes(
    memory_store: InMemoryDocumentsStore, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    from lda.modules.documents.tests.support import FaultyFactory

    inner = InMemoryDocumentsUnitOfWorkFactory(memory_store)
    rig = build_rig(inner, clock, ids)
    document_id, _ = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    faulty = build_rig(
        FaultyFactory(inner, fail_commit_on=len(inner.begun) + 1), clock, ids, rig.store
    )
    with pytest.raises(InfrastructureError):
        faulty.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert memory_store.receipts == {} and (TENANT_A, document_id) in memory_store.documents
    assert rig.purge.execute(SYSTEM_A, DocumentReference(document_id)).purged


# ---- verify ------------------------------------------------------------------------------------


def test_verify_reports_only_what_documents_can_prove(rig: Rig) -> None:
    document_id, _ = uploaded_ids(rig, CTX)
    before = rig.verify.execute(CTX, DocumentReference(document_id))
    assert (before.receipt_found, before.verified) == (False, False)
    rig.delete.execute(CTX, DocumentReference(document_id))
    assert rig.verify.execute(CTX, DocumentReference(document_id)).verified is False
    rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    after = rig.verify.execute(CTX, DocumentReference(document_id))
    assert (after.receipt_found, after.rows_absent, after.originals_absent) == (True, True, True)
    assert after.verified and after.scope == PURGE_SCOPE


def test_verify_notices_a_resurrected_original(rig: Rig) -> None:
    document_id, v1 = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    rig.store.put(key(document_id, v1), b"back", content_type=PDF)
    view = rig.verify.execute(CTX, DocumentReference(document_id))
    assert view.originals_absent is False and view.verified is False


def test_verify_is_tenant_scoped(rig: Rig) -> None:
    document_id, _ = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert (
        rig.verify.execute(ctx_for(TENANT_B), DocumentReference(document_id)).receipt_found is False
    )


def test_a_document_that_already_has_a_receipt_is_never_purged_again(rig: Rig) -> None:
    document_id, v1 = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    store: InMemoryDocumentsStore = rig.factory.store
    store.receipts[(TENANT_A, document_id)] = PurgeReceipt(
        TENANT_A, document_id, START, START, (v1,)
    )
    view = rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert (view.purged, view.versions_purged) == (False, 0)
    assert rig.store.exists(key(document_id, v1))  # nothing was touched


def test_a_version_that_appears_during_the_purge_aborts_it_and_a_retry_covers_it(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    from dataclasses import replace

    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    document_id, v1 = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    original = memory_factory.store.versions[(TENANT_A, v1)]
    late = replace(original, id=type(v1)(UUID(int=0xBEEF)), number=2)

    def late_version() -> None:
        memory_factory.store.versions[(TENANT_A, late.id)] = late
        store.after_delete = None

    store.after_delete = late_version
    with pytest.raises(ConflictError) as caught:
        rig.purge.execute(SYSTEM_A, DocumentReference(document_id))
    assert caught.value.code == "purge_version_set_changed"
    assert memory_factory.store.receipts == {}
    assert rig.purge.execute(SYSTEM_A, DocumentReference(document_id)).versions_purged == 2
