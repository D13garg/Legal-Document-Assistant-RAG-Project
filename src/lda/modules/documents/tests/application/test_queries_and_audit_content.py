"""GetDocument, and the sensitive-data guarantee across a whole lifecycle (invariant 13)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lda.modules.documents.application.dto.documents import (
    AddDocumentVersionCommand,
    DocumentReference,
)
from lda.modules.documents.testing import InMemoryDocumentsUnitOfWorkFactory
from lda.modules.documents.tests.support import (
    ALICE,
    PDF,
    START,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    Rig,
    all_recorded,
    ctx_for,
    upload_command,
)
from lda.shared.kernel.errors import NotFoundError

CTX = ctx_for(TENANT_A, ALICE)


def test_get_document_returns_metadata_and_every_version_in_order(rig: Rig) -> None:
    first = rig.upload.execute(CTX, upload_command(title="  Lease 2026  "))
    rig.clock.advance(timedelta(hours=1))
    second = rig.add_version.execute(
        CTX, AddDocumentVersionCommand(first.document_id, "text/plain", b"hello")
    )
    view = rig.get.execute(CTX, DocumentReference(first.document_id))
    assert (view.title, view.latest_version_number, view.created_at) == ("Lease 2026", 2, START)
    assert [(v.number, v.status, v.media_type, v.size_bytes) for v in view.versions] == [
        (1, "pending", PDF, len(upload_command().body)),
        (2, "pending", "text/plain", 5),
    ]
    assert [v.version_id for v in view.versions] == [first.version_id, second.version_id]


def test_get_document_does_not_cross_tenants_or_find_strangers(rig: Rig) -> None:
    view = rig.upload.execute(CTX, upload_command())
    with pytest.raises(NotFoundError) as caught:
        rig.get.execute(ctx_for(TENANT_B), DocumentReference(view.document_id))
    assert caught.value.code == "document_not_found"


def test_reads_emit_no_audit_fact_and_open_no_job(rig: Rig) -> None:
    view = rig.upload.execute(CTX, upload_command())
    before = [len(u.audit.facts) for u in rig.factory.begun]
    rig.get.execute(CTX, DocumentReference(view.document_id))
    rig.verify.execute(CTX, DocumentReference(view.document_id))
    assert [len(u.audit.facts) for u in rig.factory.begun[: len(before)]] == before
    assert all(not u.audit.facts and not u.jobs.jobs for u in rig.factory.begun[len(before) :])


def test_no_title_filename_or_body_ever_reaches_audit_events_or_jobs(rig: Rig) -> None:
    title, body = "Privileged merger memo", b"TOP-SECRET-CLAUSE-7"
    view = rig.upload.execute(CTX, upload_command(title=title, body=body))
    rig.add_version.execute(CTX, AddDocumentVersionCommand(view.document_id, PDF, b"SECOND-SECRET"))
    rig.delete.execute(CTX, DocumentReference(view.document_id))
    rig.purge.execute(SYSTEM_A, DocumentReference(view.document_id))
    recorded = repr(all_recorded(rig.factory))
    assert recorded
    for forbidden in (title, "merger", "memo", "TOP-SECRET", "SECOND-SECRET", "%PDF"):
        assert forbidden not in recorded
    # the receipt (kept forever) carries identifiers and timestamps only
    receipt = rig.factory.store.receipts[(TENANT_A, view.document_id)]
    assert title not in repr(receipt)


def test_every_recorded_fact_is_a_valid_audit_entry_shape(rig: Rig) -> None:
    """The facts must pass Audit's own validation (without Documents importing Audit): identifier
    keys, bounded printable values, resource id shaped as an opaque id."""
    import re

    view = rig.upload.execute(CTX, upload_command())
    rig.delete.execute(CTX, DocumentReference(view.document_id))
    rig.purge.execute(SYSTEM_A, DocumentReference(view.document_id))
    facts = [f for u in rig.factory.begun for f in u.audit.facts]
    assert [f.action for f in facts] == [
        "documents.document_uploaded",
        "documents.document_deleted",
        "documents.document_purged",
    ]
    for fact in facts:
        assert re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", fact.action)
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", fact.resource_id or "")
        assert len(fact.attributes) <= 16
        assert all(
            re.fullmatch(r"[a-z][a-z0-9_]*", k) and v.isprintable() and len(v) <= 256
            for k, v in fact.attributes.items()
        )
        assert not {"content", "text", "title", "filename", "name"} & set(fact.attributes)


def test_the_factory_type_is_the_in_memory_one(rig: Rig) -> None:
    assert isinstance(rig.factory, InMemoryDocumentsUnitOfWorkFactory)
