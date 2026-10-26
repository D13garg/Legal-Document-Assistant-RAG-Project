"""ExtractStage: outcomes, immutability, crash-safety, fencing, failure classification."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from uuid import UUID

import pytest

from lda.modules.document_processing.api import ExtractedDocument, ExtractionLimits
from lda.modules.document_processing.domain.extraction import content_hash
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.infrastructure.parsers.registry import FormatParsers
from lda.modules.document_processing.tests.builders import Para, make_docx, make_pdf
from lda.modules.document_processing.tests.support import (
    CSV,
    DOCX,
    PDF,
    SECRET,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    TEXT,
    Rig,
)
from lda.modules.documents.api import DocumentVersionId as SourceVersionId
from lda.modules.documents.api import VersionStatus
from lda.modules.ingestion.api import StagePermanentFailure, StageSucceeded
from lda.shared.kernel.errors import ConflictError, InfrastructureError

BODY = f"{SECRET}\n\nSecond paragraph.".encode()


class OtherParserVersion(FormatParsers):
    @property
    def version(self) -> str:
        return "extract-2; test"


def stored_artifact(rig: Rig, document: DocumentId, version: VersionId) -> ExtractedDocument:
    return ExtractedDocument.from_bytes(rig.objects.get(rig.key(document, version)))


def failure_code(
    rig: Rig, body: bytes, media_type: str, limits: ExtractionLimits | None = None
) -> str:
    document, version = rig.upload_document(body, media_type)
    stage = rig.make_stage(FormatParsers(), limits)
    outcome = stage.run(rig.request(document, version))
    assert isinstance(outcome, StagePermanentFailure)
    assert not rig.store.extractions  # nothing recorded for a failure
    assert not rig.objects.derived_keys()
    return outcome.error_code


# ---- success -----------------------------------------------------------------------------------


def test_text_document_is_extracted_recorded_and_stored(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    outcome = rig.stage.run(rig.request(document, version))
    assert outcome == StageSucceeded()
    artifact = stored_artifact(rig, document, version)
    assert [b.text for b in artifact.blocks] == [SECRET, "Second paragraph."]
    record = rig.store.extractions[(rig.request(document, version).tenant_id, version)]
    assert record.document_id == document
    assert record.content_hash == content_hash(artifact.to_bytes())
    assert (record.block_count, record.character_count, record.page_count) == (2, 46, None)
    assert record.media_type == TEXT
    assert record.created_at == rig.clock.now()
    assert SECRET not in repr(record)


def test_pdf_and_docx_documents(rig: Rig) -> None:
    pdf_doc, pdf_ver = rig.upload_document(make_pdf([["Page one"], ["Page two"]]), PDF)
    docx_doc, docx_ver = rig.upload_document(
        make_docx([Para("Heading", "Heading1"), Para("Body")]), DOCX
    )
    assert rig.stage.run(rig.request(pdf_doc, pdf_ver)) == StageSucceeded()
    assert rig.stage.run(rig.request(docx_doc, docx_ver)) == StageSucceeded()
    assert stored_artifact(rig, pdf_doc, pdf_ver).page_count == 2
    assert stored_artifact(rig, docx_doc, docx_ver).blocks[0].level == 1


def test_stage_name_and_content_type(rig: Rig) -> None:
    assert rig.stage.name == "extract"
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    path, overwrite = next(c for c in rig.objects.put_calls if "document_processing/" in c[0])
    assert path.endswith(f"{document}/{version}/extracted") and overwrite is False


def test_success_never_changes_the_version_status(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    with rig.documents.begin(SYSTEM_A) as uow:
        stored = uow.versions.get(SourceVersionId(version.value))
        assert stored is not None and stored.status is VersionStatus.PENDING


def test_processing_writes_no_event_job_or_audit(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    assert rig.processing.begun  # used the database, but records nothing else


# ---- idempotency and crash safety --------------------------------------------------------------


def test_rerun_is_idempotent_and_never_rewrites(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    request = rig.request(document, version)
    assert rig.stage.run(request) == StageSucceeded()
    first_record = dict(rig.store.extractions)
    first_bytes = rig.objects.get(rig.key(document, version))
    rig.clock.advance(timedelta(seconds=60))
    assert rig.stage.run(request) == StageSucceeded()
    assert rig.store.extractions == first_record  # same row, original timestamp
    assert rig.objects.get(rig.key(document, version)) == first_bytes


def test_crash_before_commit_leaves_nothing(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.store.fail_commit_on = {len(rig.processing.begun)}  # the effect transaction
    with pytest.raises(InfrastructureError):
        rig.stage.run(rig.request(document, version))
    assert not rig.store.extractions
    assert not rig.objects.derived_keys()
    rig.store.fail_commit_on = set()
    assert rig.stage.run(rig.request(document, version)) == StageSucceeded()


def test_crash_after_row_before_object_is_completed_by_rerun(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.objects.fail_put = True
    with pytest.raises(InfrastructureError):
        rig.stage.run(rig.request(document, version))
    assert len(rig.store.extractions) == 1  # findable by purge
    assert not rig.objects.derived_keys()
    rig.objects.fail_put = False
    assert rig.stage.run(rig.request(document, version)) == StageSucceeded()
    assert stored_artifact(rig, document, version).blocks


def test_crash_after_object_before_return_is_idempotent(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)

    def crash() -> None:
        raise InfrastructureError("worker_died")

    rig.objects.after_put = crash
    with pytest.raises(InfrastructureError):
        rig.stage.run(rig.request(document, version))
    rig.objects.after_put = None
    assert rig.stage.run(rig.request(document, version)) == StageSucceeded()


def test_database_and_storage_outages_are_retryable_exceptions(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.objects.fail_get = True
    with pytest.raises(InfrastructureError):
        rig.stage.run(rig.request(document, version))
    assert not rig.store.extractions


def test_stale_fence_records_and_writes_nothing(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.store.fence_rejects = lambda job, token: token == 1
    with pytest.raises(ConflictError):
        rig.stage.run(rig.request(document, version, token=1))
    assert not rig.store.extractions
    assert not rig.objects.derived_keys()
    assert rig.stage.run(rig.request(document, version, token=2)) == StageSucceeded()


# ---- immutability ------------------------------------------------------------------------------


def test_conflicting_existing_object_is_never_overwritten(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.objects.put(rig.key(document, version), b"{}", content_type="application/json")
    outcome = rig.stage.run(rig.request(document, version))
    assert outcome == StagePermanentFailure("artifact_conflict")
    assert rig.objects.get(rig.key(document, version)) == b"{}"


def test_same_parser_different_recorded_hash_is_a_conflict(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    key = next(iter(rig.store.extractions))
    rig.store.extractions[key] = dataclasses.replace(
        rig.store.extractions[key], content_hash="0" * 64
    )
    assert rig.stage.run(rig.request(document, version)) == StagePermanentFailure(
        "artifact_conflict"
    )


def test_tampered_object_under_a_recorded_hash_is_a_conflict(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    rig.objects.put(
        rig.key(document, version), b"{}", content_type="application/json", overwrite=True
    )
    assert rig.stage.run(rig.request(document, version)) == StagePermanentFailure(
        "artifact_conflict"
    )


def test_other_parser_version_keeps_the_recorded_output(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    before_row = dict(rig.store.extractions)
    before_bytes = rig.objects.get(rig.key(document, version))
    upgraded = rig.make_stage(OtherParserVersion())
    assert upgraded.run(rig.request(document, version)) == StageSucceeded()
    assert rig.store.extractions == before_row  # still the old parser version
    assert rig.objects.get(rig.key(document, version)) == before_bytes


def test_other_parser_version_with_missing_object_is_unrecoverable(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    rig.objects.delete(rig.key(document, version))
    upgraded = rig.make_stage(OtherParserVersion())
    assert upgraded.run(rig.request(document, version)) == StagePermanentFailure(
        "artifact_unrecoverable"
    )
    assert not rig.objects.derived_keys()


def test_other_parser_version_with_tampered_object_is_a_conflict(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    rig.objects.put(
        rig.key(document, version), b"{}", content_type="application/json", overwrite=True
    )
    upgraded = rig.make_stage(OtherParserVersion())
    assert upgraded.run(rig.request(document, version)) == StagePermanentFailure(
        "artifact_conflict"
    )


def test_same_parser_with_missing_object_rewrites_the_identical_artifact(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    expected = rig.objects.get(rig.key(document, version))
    rig.objects.delete(rig.key(document, version))
    assert rig.stage.run(rig.request(document, version)) == StageSucceeded()
    assert rig.objects.get(rig.key(document, version)) == expected


# ---- permanent failures ------------------------------------------------------------------------


def test_unsupported_media_type(rig: Rig) -> None:
    assert failure_code(rig, b"a,b\n1,2\n", CSV) == "unsupported_media_type"


def test_malformed_documents(rig: Rig) -> None:
    assert failure_code(rig, b"%PDF-1.4 definitely not a pdf", PDF) == "malformed_document"
    assert failure_code(rig, b"PK\x03\x04 nope", DOCX) == "malformed_document"
    assert failure_code(rig, b"caf\xe9", TEXT) == "malformed_document"


def test_empty_and_textless_documents(rig: Rig) -> None:
    assert failure_code(rig, b" \n\t ", TEXT) == "empty_document"
    assert failure_code(rig, make_pdf([None]), PDF) == "no_extractable_text"


def test_limits_are_enforced(rig: Rig) -> None:
    tight = ExtractionLimits(max_bytes=20, max_pages=1, max_characters=5, max_expanded_bytes=1000)
    assert failure_code(rig, b"x" * 21, TEXT, tight) == "limit_exceeded.bytes"
    assert failure_code(rig, b"x" * 6, TEXT, tight) == "limit_exceeded.characters"
    roomy = ExtractionLimits(10**6, 1, 1000, 10**6)
    assert failure_code(rig, make_pdf([["a"], ["b"]]), PDF, roomy) == "limit_exceeded.pages"


def test_failure_outcomes_carry_only_a_code(rig: Rig) -> None:
    document, version = rig.upload_document(b"%PDF-1.4 " + SECRET.encode(), PDF)
    outcome = rig.stage.run(rig.request(document, version))
    assert isinstance(outcome, StagePermanentFailure)
    assert SECRET not in repr(outcome)


# ---- the version is not (or no longer) processable ---------------------------------------------


def test_unknown_version_and_foreign_tenant(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    unknown = VersionId(UUID(int=0xDEAD))
    assert rig.stage.run(rig.request(document, unknown)) == StagePermanentFailure(
        "version_unavailable"
    )
    assert rig.stage.run(rig.request(document, version, tenant=TENANT_B)) == StagePermanentFailure(
        "version_unavailable"
    )
    assert not rig.store.extractions


def test_tombstoned_document_is_unavailable(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.tombstone(document)
    assert rig.stage.run(rig.request(document, version)) == StagePermanentFailure(
        "version_unavailable"
    )
    assert not rig.store.extractions


@pytest.mark.parametrize("status", [VersionStatus.READY, VersionStatus.FAILED])
def test_version_that_left_pending_is_unavailable(rig: Rig, status: VersionStatus) -> None:
    document, version = rig.upload_document(BODY)
    rig.mark(document, version, status)
    assert rig.stage.run(rig.request(document, version)) == StagePermanentFailure(
        "version_unavailable"
    )


def test_missing_original_is_its_own_failure(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.objects.remove_originals()
    assert rig.stage.run(rig.request(document, version)) == StagePermanentFailure(
        "original_missing"
    )


def test_original_with_wrong_size_is_a_retryable_infrastructure_error(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.objects.replace_originals(b"short")
    with pytest.raises(InfrastructureError):
        rig.stage.run(rig.request(document, version))


def test_two_tenants_get_separate_records_and_objects(rig: Rig) -> None:
    doc_a, ver_a = rig.upload_document(BODY)
    doc_b, ver_b = rig.upload_document(BODY, tenant=TENANT_B)
    assert rig.stage.run(rig.request(doc_a, ver_a)) == StageSucceeded()
    assert rig.stage.run(rig.request(doc_b, ver_b, tenant=TENANT_B)) == StageSucceeded()
    assert {tenant for tenant, _ in rig.store.extractions} == {TENANT_A, TENANT_B}
    assert rig.objects.exists(rig.key(doc_b, ver_b, TENANT_B))
    assert not rig.objects.exists(rig.key(doc_b, ver_b, TENANT_A))


def test_a_recorded_row_that_vanishes_mid_reconcile_is_retried(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Insert-if-absent says a row exists but the read finds none (deleted in between): a
    retryable infrastructure error, never a silent success and never a permanent failure."""
    from lda.modules.document_processing.testing.in_memory import InMemoryExtractionRepository

    document, version = rig.upload_document(BODY)
    monkeypatch.setattr(InMemoryExtractionRepository, "add", lambda self, record: False)
    monkeypatch.setattr(InMemoryExtractionRepository, "get", lambda self, version_id: None)
    with pytest.raises(InfrastructureError) as excinfo:
        rig.stage.run(rig.request(document, version))
    assert excinfo.value.code == "extraction_record_vanished"
    assert not rig.objects.exists(rig.key(document, version))
