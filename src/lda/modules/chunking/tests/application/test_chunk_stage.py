"""ChunkStage: outcomes, immutability, crash-safety, fencing, failure classification, lifecycle."""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta

import pytest

from lda.modules.chunking.api import ChunkingParameters, PurgeCommand
from lda.modules.chunking.domain.chunk import SEPARATOR, content_hash
from lda.modules.chunking.domain.failures import (
    ARTIFACT_CONFLICT,
    ARTIFACT_UNRECOVERABLE,
    EXTRACTION_CORRUPT,
    EXTRACTION_UNAVAILABLE,
    VERSION_UNAVAILABLE,
)
from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.record import PurgeReceipt
from lda.modules.chunking.tests.support import (
    SECRET,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    Rig,
)
from lda.modules.document_processing.api import DocumentId as ProcDocumentId
from lda.modules.document_processing.api import ReadExtractionCommand
from lda.modules.document_processing.api import VersionId as ProcVersionId
from lda.modules.documents.api import VersionStatus
from lda.modules.ingestion.api import StagePermanentFailure, StageSucceeded
from lda.shared.kernel.errors import ConflictError, InfrastructureError

BODY = f"# Indemnity\n\n{SECRET}\n\n## Scope\n\nSecond paragraph of the agreement."


def stored(rig: Rig, document: DocumentId, version: VersionId) -> dict[str, object]:
    return json.loads(rig.objects.get(rig.key(document, version)))  # type: ignore[no-any-return]


def failure(outcome: object) -> str:
    assert isinstance(outcome, StagePermanentFailure)
    return outcome.error_code


# ---- success ------------------------------------------------------------------------------------


def test_chunks_are_recorded_and_stored_with_provenance(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    assert rig.stage.run(rig.request(document, version)) == StageSucceeded()
    payload = stored(rig, document, version)
    record = rig.store.chunk_sets[(TENANT_A, version)]
    extraction = rig.processing.store.extractions[(TENANT_A, ProcVersionId(version.value))]
    assert payload["extraction_hash"] == extraction.content_hash == record.extraction_hash
    assert record.content_hash == content_hash(rig.objects.get(rig.key(document, version)))
    assert record.chunker_version == rig.stage._parameters.version  # noqa: SLF001
    assert record.chunk_count == len(payload["chunks"])  # type: ignore[arg-type]
    assert record.created_at == rig.clock.now()
    assert SECRET not in repr(record)


def test_the_stage_name_and_the_artifact_is_written_without_overwrite(rig: Rig) -> None:
    assert rig.stage.name == "chunk"
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    (path, overwrite) = next(c for c in rig.objects.put_calls if "chunking/" in c[0])
    assert path.endswith(f"/chunking/{document}/{version}/chunks")
    assert overwrite is False


def test_chunk_boundaries_reproduce_the_text_from_the_extraction(rig: Rig) -> None:
    document, version = rig.upload_document(BODY + "\n\n" + "Long sentence here. " * 40)
    rig.stage.run(rig.request(document, version))
    blocks = rig.reader.execute(
        SYSTEM_A,
        ReadExtractionCommand(ProcDocumentId(document.value), ProcVersionId(version.value)),
    ).extracted.blocks
    payload = stored(rig, document, version)
    assert len(payload["chunks"]) > 2  # type: ignore[arg-type]
    for chunk in payload["chunks"]:  # type: ignore[attr-defined]
        rebuilt = SEPARATOR.join(blocks[b].text[s:e] for b, s, e in chunk["segments"])
        assert rebuilt == chunk["text"]
        assert len(chunk["text"]) <= 200


def test_two_runs_over_the_same_extraction_give_identical_bytes(rig: Rig) -> None:
    first_doc, first_ver = rig.upload_document(BODY)
    second = rig.make_stage(rig.stage._parameters)  # noqa: SLF001
    rig.stage.run(rig.request(first_doc, first_ver))
    before = rig.objects.get(rig.key(first_doc, first_ver))
    assert second.run(rig.request(first_doc, first_ver)) == StageSucceeded()
    assert rig.objects.get(rig.key(first_doc, first_ver)) == before


# ---- idempotency, retries, crashes --------------------------------------------------------------


def test_a_duplicate_job_changes_nothing(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    request = rig.request(document, version)
    assert rig.stage.run(request) == StageSucceeded()
    snapshot = dict(rig.store.chunk_sets), rig.objects.get(rig.key(document, version))
    assert rig.stage.run(request) == StageSucceeded()
    assert (dict(rig.store.chunk_sets), rig.objects.get(rig.key(document, version))) == snapshot


def test_object_store_failure_after_the_row_is_retried_and_completed(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.objects.fail_put = True
    with pytest.raises(InfrastructureError):
        rig.stage.run(rig.request(document, version))
    assert (TENANT_A, version) in rig.store.chunk_sets  # row first, object second
    assert not rig.objects.derived_keys()
    rig.objects.fail_put = False
    assert rig.stage.run(rig.request(document, version)) == StageSucceeded()
    assert len(rig.objects.derived_keys()) == 1


def test_a_failed_commit_leaves_nothing_and_a_retry_succeeds(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.store.fail_commit_on = {len(rig.chunking.begun)}  # the record transaction
    with pytest.raises(InfrastructureError):
        rig.stage.run(rig.request(document, version))
    assert not rig.store.chunk_sets and not rig.objects.derived_keys()
    rig.store.fail_commit_on = set()
    assert rig.stage.run(rig.request(document, version)) == StageSucceeded()


def test_a_crashed_run_that_already_wrote_the_object_is_adopted(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.objects.fail_get = False
    rig.stage.run(rig.request(document, version))
    del rig.store.chunk_sets[(TENANT_A, version)]  # simulate: object written, row lost
    assert rig.stage.run(rig.request(document, version)) == StageSucceeded()
    assert (TENANT_A, version) in rig.store.chunk_sets


def test_a_lost_fence_records_nothing(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)

    def reject(_job: object, _token: int) -> bool:
        return True

    rig.store.fence_rejects = reject
    with pytest.raises(ConflictError):
        rig.stage.run(rig.request(document, version))
    assert not rig.store.chunk_sets and not rig.objects.derived_keys()


def test_every_writer_takes_the_lifecycle_lock(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    assert any(document in uow.locked for uow in rig.chunking.begun)


# ---- immutability and reconciliation ------------------------------------------------------------


def test_another_chunker_version_keeps_the_recorded_set(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    before = rig.objects.get(rig.key(document, version))
    recorded = rig.store.chunk_sets[(TENANT_A, version)]
    other = rig.make_stage(ChunkingParameters(150, 30, 1000))
    assert other.run(rig.request(document, version)) == StageSucceeded()
    assert rig.objects.get(rig.key(document, version)) == before
    assert rig.store.chunk_sets[(TENANT_A, version)] == recorded


def test_the_same_chunker_with_a_different_result_is_a_conflict(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    key = (TENANT_A, version)
    rig.store.chunk_sets[key] = dataclasses.replace(
        rig.store.chunk_sets[key], content_hash="f" * 64
    )
    assert failure(rig.stage.run(rig.request(document, version))) == ARTIFACT_CONFLICT


def test_a_tampered_object_is_never_overwritten(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    rig.objects._objects[rig.key(document, version).render()] = b"{}"  # noqa: SLF001
    assert failure(rig.stage.run(rig.request(document, version))) == ARTIFACT_CONFLICT
    assert rig.objects.get(rig.key(document, version)) == b"{}"


def test_a_vanished_object_under_another_chunker_is_unrecoverable(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    rig.objects.delete(rig.key(document, version))
    other = rig.make_stage(ChunkingParameters(150, 30, 1000))
    assert failure(other.run(rig.request(document, version))) == ARTIFACT_UNRECOVERABLE


# ---- failure classification ---------------------------------------------------------------------


def test_a_version_that_was_never_extracted_fails_permanently(rig: Rig) -> None:
    document, version = rig.upload_document(BODY, extract=False)
    assert failure(rig.stage.run(rig.request(document, version))) == EXTRACTION_UNAVAILABLE
    assert not rig.store.chunk_sets and not rig.objects.derived_keys()


def test_a_missing_extraction_artifact_fails_permanently(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    (key,) = [k for k in rig.objects._objects if "document_processing" in k]  # noqa: SLF001
    del rig.objects._objects[key]  # noqa: SLF001
    assert failure(rig.stage.run(rig.request(document, version))) == EXTRACTION_UNAVAILABLE


def test_a_corrupt_extraction_fails_permanently(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    (key,) = [k for k in rig.objects._objects if "document_processing" in k]  # noqa: SLF001
    rig.objects._objects[key] = b"{}"  # noqa: SLF001
    assert failure(rig.stage.run(rig.request(document, version))) == EXTRACTION_CORRUPT


@pytest.mark.parametrize("status", [VersionStatus.FAILED, VersionStatus.QUARANTINED])
def test_a_version_that_is_not_eligible_fails_permanently(rig: Rig, status: VersionStatus) -> None:
    document, version = rig.upload_document(BODY)
    rig.mark(document, version, status)
    assert failure(rig.stage.run(rig.request(document, version))) == VERSION_UNAVAILABLE
    assert not rig.store.chunk_sets


def test_exceeding_the_chunk_limit_fails_permanently_and_records_nothing(rig: Rig) -> None:
    document, version = rig.upload_document("x " * 300)  # needs several chunks
    stage = rig.make_stage(ChunkingParameters(100, 50, 1))  # one chunk allowed
    assert failure(stage.run(rig.request(document, version))) == "limit_exceeded.chunks"
    assert not rig.store.chunk_sets and not rig.objects.derived_keys()


def test_a_store_failure_reading_the_extraction_is_retried_not_classified(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.objects.fail_get = True
    with pytest.raises(InfrastructureError):
        rig.stage.run(rig.request(document, version))
    assert not rig.store.chunk_sets


# ---- tenants ------------------------------------------------------------------------------------


def test_another_tenant_cannot_chunk_or_see_a_version(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    outcome = rig.stage.run(rig.request(document, version, tenant=TENANT_B))
    assert failure(outcome) == VERSION_UNAVAILABLE
    assert not rig.store.chunk_sets and not rig.objects.derived_keys()


def test_two_tenants_keep_separate_sets_under_separate_keys(rig: Rig) -> None:
    doc_a, ver_a = rig.upload_document(BODY)
    doc_b, ver_b = rig.upload_document(BODY, tenant=TENANT_B)
    rig.stage.run(rig.request(doc_a, ver_a))
    rig.stage.run(rig.request(doc_b, ver_b, tenant=TENANT_B))
    assert {t for t, _ in rig.store.chunk_sets} == {TENANT_A, TENANT_B}
    assert len(rig.objects.derived_keys()) == 2
    a, b = stored(rig, doc_a, ver_a), json.loads(rig.objects.get(rig.key(doc_b, ver_b, TENANT_B)))
    assert {c["id"] for c in a["chunks"]}.isdisjoint(c["id"] for c in b["chunks"])  # type: ignore[attr-defined]


# ---- lifecycle ----------------------------------------------------------------------------------


def test_a_deleted_document_is_refused_and_nothing_is_derived(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.tombstone(document)
    assert failure(rig.stage.run(rig.request(document, version))) == VERSION_UNAVAILABLE
    assert not rig.store.chunk_sets and not rig.objects.derived_keys()


def test_deletion_after_chunking_leaves_a_set_the_purge_removes(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    rig.stage.run(rig.request(document, version))
    rig.tombstone(document)
    rig.clock.advance(timedelta(seconds=1))
    view = rig.purge.execute(SYSTEM_A, PurgeCommand(document))
    assert (view.purged, view.versions_purged) == (True, 1)
    assert not rig.store.chunk_sets and not rig.objects.derived_keys()


def test_nothing_is_recorded_once_the_purge_receipt_exists(rig: Rig) -> None:
    """A stale job whose document Documents still reports as active: only the receipt stops it."""
    document, version = rig.upload_document(BODY)
    with rig.chunking.begin(SYSTEM_A) as uow:
        uow.receipts.add(PurgeReceipt(TENANT_A, document, rig.clock.now(), ()))
        uow.commit()
    assert failure(rig.stage.run(rig.request(document, version))) == VERSION_UNAVAILABLE
    assert not rig.store.chunk_sets and not rig.objects.derived_keys()


def test_a_purge_that_finishes_during_the_object_write_removes_the_late_object(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)

    def purge_now() -> (
        None
    ):  # runs right after the stage's put: the row exists, the receipt not yet
        rig.tombstone(document)
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))

    rig.objects.after_put = purge_now
    outcome = rig.stage.run(rig.request(document, version))
    assert failure(outcome) == VERSION_UNAVAILABLE
    assert not rig.objects.derived_keys()  # no orphan survives the receipt
    assert not rig.store.chunk_sets


def test_a_failed_cleanup_is_finished_by_the_retry(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)

    def purge_then_break_delete() -> None:
        rig.tombstone(document)
        rig.purge.execute(SYSTEM_A, PurgeCommand(document))
        rig.objects._objects[rig.key(document, version).render()] = b"orphan"  # noqa: SLF001
        rig.objects.fail_delete = True

    rig.objects.after_put = purge_then_break_delete
    with pytest.raises(InfrastructureError):
        rig.stage.run(rig.request(document, version))
    assert rig.objects.derived_keys()  # the orphan is there, the receipt too
    rig.objects.fail_delete = False
    assert failure(rig.stage.run(rig.request(document, version))) == VERSION_UNAVAILABLE
    assert not rig.objects.derived_keys()
