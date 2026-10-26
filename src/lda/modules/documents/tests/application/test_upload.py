"""Upload protocol (ADR-005): intent, object, rows; every failure leaves a recoverable state."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lda.modules.documents.application.dto.documents import (
    AddDocumentVersionCommand,
    UploadDocumentCommand,
)
from lda.modules.documents.application.jobs import DOCUMENTS_QUEUE, UPLOAD_INTENT_JOB
from lda.modules.documents.domain.document import DocumentStatus
from lda.modules.documents.domain.events import DocumentVersionAdded
from lda.modules.documents.domain.resource import original_path
from lda.modules.documents.testing import InMemoryDocumentsStore, InMemoryDocumentsUnitOfWorkFactory
from lda.modules.documents.tests.support import (
    ALICE,
    BODY,
    PDF,
    POLICY,
    START,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    FaultyFactory,
    FlakyStore,
    Rig,
    a_document_id,
    build_rig,
    ctx_for,
    upload_command,
    uploaded_ids,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import (
    ConflictError,
    InfrastructureError,
    NotFoundError,
    ValidationError,
)
from lda.shared.ports.audit import AuditOutcome
from lda.shared.ports.events import Delivery
from lda.shared.ports.object_store import TenantScopedKey

CTX = ctx_for(TENANT_A, ALICE, 7)


def key_of(rig: Rig, document_id, version_id) -> TenantScopedKey:  # type: ignore[no-untyped-def]
    return TenantScopedKey(TENANT_A, original_path(document_id, version_id))


def test_upload_creates_the_document_its_pending_first_version_and_the_object(rig: Rig) -> None:
    view = rig.upload.execute(CTX, upload_command())
    store = rig.factory.store
    document = store.documents[(TENANT_A, view.document_id)]
    version = store.versions[(TENANT_A, view.version_id)]
    assert (view.version_number, view.status) == (1, "pending")
    assert (document.title, document.status, document.created_by) == (
        "Master services agreement",
        DocumentStatus.ACTIVE,
        ALICE,
    )
    assert (version.number, version.size_bytes, version.media_type) == (1, len(BODY), PDF)
    assert rig.store.get(key_of(rig, view.document_id, view.version_id)) == BODY
    assert store.intents == {}  # the intent is consumed by the same transaction that adds the rows


def test_the_intent_is_committed_before_the_object_is_written(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    seen: list[int] = []
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    store.after_put = lambda: seen.append(len(memory_factory.store.intents))
    rig.upload.execute(CTX, upload_command())
    assert seen == [1]  # at the moment the bytes landed, a durable intent already existed


def test_the_delayed_cleanup_job_is_enqueued_with_the_intent(rig: Rig) -> None:
    view = rig.upload.execute(CTX, upload_command())
    (spec,) = [j for u in rig.factory.begun for j in u.jobs.jobs]
    assert (spec.queue, spec.job_type) == (DOCUMENTS_QUEUE, UPLOAD_INTENT_JOB)
    assert spec.payload == {
        "document_id": str(view.document_id),
        "version_id": str(view.version_id),
    }
    assert spec.unique_key == f"{UPLOAD_INTENT_JOB}:{view.version_id}"
    assert spec.run_at == START + timedelta(seconds=POLICY.grace_seconds)


def test_upload_audits_one_identifier_only_fact_and_publishes_one_event(rig: Rig) -> None:
    view = rig.upload.execute(CTX, upload_command(title="Secret merger memo"))
    uows = [u for u in rig.factory.begun if u.audit.facts]
    (fact,) = [f for u in uows for f in u.audit.facts]
    assert (fact.action, fact.resource_type, fact.resource_id) == (
        "documents.document_uploaded",
        "document",
        str(view.document_id),
    )
    assert (fact.actor, fact.outcome, fact.occurred_at) == (ALICE, AuditOutcome.SUCCESS, START)
    assert dict(fact.attributes) == {
        "version_id": str(view.version_id),
        "version_number": "1",
        "media_type": PDF,
        "size_bytes": str(len(BODY)),
    }
    (event, delivery) = next(e for u in uows for e in u.events.events)
    assert isinstance(event, DocumentVersionAdded) and delivery is Delivery.ASYNC_JOB
    assert (event.document_id, event.version_id, event.version_number) == (
        view.document_id,
        view.version_id,
        1,
    )


def test_system_uploads_have_no_creator_and_record_their_purpose(rig: Rig) -> None:
    view = rig.upload.execute(SYSTEM_A, upload_command())
    store = rig.factory.store
    assert store.documents[(TENANT_A, view.document_id)].created_by is None
    fact = next(f for u in rig.factory.begun for f in u.audit.facts)
    assert fact.actor is None and fact.attributes["purpose"] == "documents-test"


@pytest.mark.parametrize(
    ("command", "code"),
    [
        (UploadDocumentCommand("", PDF, BODY), "document_title_invalid"),
        (UploadDocumentCommand("t", "image/png", BODY), "document_media_type_not_allowed"),
        (UploadDocumentCommand("t", PDF, b""), "document_empty"),
        (UploadDocumentCommand("t", PDF, b"x" * 1001), "document_too_large"),
    ],
)
def test_invalid_input_writes_nothing_at_all(
    rig: Rig, command: UploadDocumentCommand, code: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        rig.upload.execute(CTX, command)
    assert caught.value.code == code
    assert rig.factory.begun == []  # not even an intent
    assert not rig.store._objects  # noqa: SLF001


def test_the_body_never_appears_in_a_repr(rig: Rig) -> None:
    assert "confidential" not in repr(upload_command())
    assert "confidential" not in repr(
        AddDocumentVersionCommand(document_id=a_document_id(), media_type=PDF, body=BODY)
    )


def test_a_failure_while_committing_the_rows_discards_the_object_and_keeps_the_intent(
    memory_store: InMemoryDocumentsStore, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    inner = InMemoryDocumentsUnitOfWorkFactory(memory_store)
    store = FlakyStore()
    rig = build_rig(FaultyFactory(inner, fail_commit_on=1), clock, ids, store)
    with pytest.raises(InfrastructureError):
        rig.upload.execute(CTX, upload_command())
    assert not store._objects  # noqa: SLF001  # immediate compensation removed the bytes
    assert memory_store.documents == {} and memory_store.versions == {}
    assert len(memory_store.intents) == 1  # ... and the durable intent still guarantees cleanup


def test_if_even_the_compensation_fails_the_object_is_left_for_the_intent_job(
    memory_store: InMemoryDocumentsStore, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    inner = InMemoryDocumentsUnitOfWorkFactory(memory_store)
    store = FlakyStore()
    store.fail_delete = True
    rig = build_rig(FaultyFactory(inner, fail_commit_on=1), clock, ids, store)
    with pytest.raises(InfrastructureError) as caught:
        rig.upload.execute(CTX, upload_command())
    assert caught.value.code == "database_unavailable"  # the ORIGINAL failure is what surfaces
    assert len(store._objects) == 1 and len(memory_store.intents) == 1  # noqa: SLF001


def test_a_failed_put_leaves_only_a_harmless_intent(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    store.fail_put = True
    rig = build_rig(memory_factory, clock, ids, store)
    with pytest.raises(InfrastructureError):
        rig.upload.execute(CTX, upload_command())
    assert memory_factory.store.documents == {} and not store._objects  # noqa: SLF001
    assert len(memory_factory.store.intents) == 1


def test_if_the_intent_job_won_the_race_the_upload_fails_and_removes_its_object(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)

    def reaper_acts_first() -> None:
        memory_factory.store.intents.clear()

    store.after_put = reaper_acts_first
    with pytest.raises(ConflictError) as caught:
        rig.upload.execute(CTX, upload_command())
    assert caught.value.code == "upload_expired"
    assert not store._objects and memory_factory.store.documents == {}  # noqa: SLF001


def test_add_version_numbers_and_audits_the_next_version(rig: Rig) -> None:
    document_id, _ = uploaded_ids(rig, CTX)
    rig.clock.advance(timedelta(hours=1))
    view = rig.add_version.execute(CTX, AddDocumentVersionCommand(document_id, "text/plain", b"v2"))
    store = rig.factory.store
    assert (
        view.version_number == 2
        and store.documents[(TENANT_A, document_id)].latest_version_number == 2
    )
    assert rig.store.get(key_of(rig, document_id, view.version_id)) == b"v2"
    fact = [f for u in rig.factory.begun for f in u.audit.facts][-1]
    assert fact.action == "documents.document_version_added"
    assert dict(fact.attributes)["version_number"] == "2"
    assert fact.occurred_at == START + timedelta(hours=1)
    assert [
        v.number for (_, _), v in sorted(store.versions.items(), key=lambda i: i[1].number)
    ] == [1, 2]


def test_versions_are_independent_immutable_objects(rig: Rig) -> None:
    document_id, first = uploaded_ids(rig, CTX)
    second = rig.add_version.execute(
        CTX, AddDocumentVersion_command(document_id, b"other")
    ).version_id
    assert rig.store.get(key_of(rig, document_id, first)) == BODY
    assert rig.store.get(key_of(rig, document_id, second)) == b"other"


def AddDocumentVersion_command(document_id, body: bytes) -> AddDocumentVersionCommand:  # type: ignore[no-untyped-def]
    return AddDocumentVersionCommand(document_id, PDF, body)


def test_adding_a_version_to_an_unknown_or_foreign_document_writes_no_bytes(rig: Rig) -> None:
    document_id, _ = uploaded_ids(rig, CTX)
    begun = len(rig.factory.begun)
    for ctx in (ctx_for(TENANT_B),):
        with pytest.raises(NotFoundError):
            rig.add_version.execute(ctx, AddDocumentVersion_command(document_id, b"x"))
    assert len(rig.store._objects) == 1  # noqa: SLF001
    assert len(rig.factory.begun) == begun + 1  # only the read


def test_adding_a_version_to_a_tombstoned_document_is_refused_before_any_write(rig: Rig) -> None:
    from lda.modules.documents.application.dto.documents import DocumentReference

    document_id, _ = uploaded_ids(rig, CTX)
    rig.delete.execute(CTX, DocumentReference(document_id))
    with pytest.raises(ConflictError) as caught:
        rig.add_version.execute(CTX, AddDocumentVersion_command(document_id, b"x"))
    assert caught.value.code == "document_deleted"
    assert len(rig.store._objects) == 1  # noqa: SLF001
    assert len(rig.factory.store.intents) == 0


def test_a_delete_that_lands_after_the_precheck_still_blocks_the_version(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    from lda.modules.documents.application.dto.documents import DocumentReference

    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    document_id, _ = uploaded_ids(rig, CTX)

    def delete_meanwhile() -> None:
        rig.delete.execute(CTX, DocumentReference(document_id))

    store.after_put = delete_meanwhile
    with pytest.raises(ConflictError) as caught:
        rig.add_version.execute(CTX, AddDocumentVersion_command(document_id, b"late"))
    assert caught.value.code == "document_deleted"
    assert len(store._objects) == 1  # noqa: SLF001  # only v1 remains; the late bytes were discarded
    assert len(memory_factory.store.versions) == 1


def test_originals_are_never_written_over_an_existing_object(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    document_id, _ = uploaded_ids(rig, CTX)
    rig.add_version.execute(CTX, AddDocumentVersionCommand(document_id, PDF, b"v2"))
    assert store.put_overwrite_flags == [False, False]  # ADR-005: immutable originals


def test_adding_a_version_fails_and_cleans_up_if_the_intent_job_won_the_race(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    store = FlakyStore()
    rig = build_rig(memory_factory, clock, ids, store)
    document_id, _ = uploaded_ids(rig, CTX)
    store.after_put = lambda: memory_factory.store.intents.clear()
    with pytest.raises(ConflictError) as caught:
        rig.add_version.execute(CTX, AddDocumentVersionCommand(document_id, PDF, b"late"))
    assert caught.value.code == "upload_expired"
    assert len(store._objects) == 1 and len(memory_factory.store.versions) == 1  # noqa: SLF001
