"""ReadExtraction: Documents' verdict first, then the row, then a verified artifact."""

from __future__ import annotations

import dataclasses

import pytest

from lda.modules.document_processing.api import (
    EXTRACTION_ARTIFACT_CORRUPT,
    EXTRACTION_ARTIFACT_MISSING,
    EXTRACTION_NOT_FOUND,
    EXTRACTION_VERSION_NOT_ELIGIBLE,
    ReadExtractionCommand,
)
from lda.modules.document_processing.domain.extraction import content_hash
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.tests.support import (
    SECRET,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    Rig,
)
from lda.modules.documents.api import VersionStatus
from lda.modules.ingestion.api import StageSucceeded
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError, NotFoundError

BODY = f"{SECRET}\n\nSecond paragraph.".encode()


def extracted(rig: Rig) -> tuple[DocumentId, VersionId]:
    document, version = rig.upload_document(BODY)
    assert rig.stage.run(rig.request(document, version)) == StageSucceeded()
    return document, version


def read(rig: Rig, document: DocumentId, version: VersionId, ctx: SystemContext = SYSTEM_A):  # type: ignore[no-untyped-def]
    return rig.read_extraction.execute(ctx, ReadExtractionCommand(document, version))


def test_returns_the_verified_extraction_of_a_pending_version(rig: Rig) -> None:
    document, version = extracted(rig)
    result = read(rig, document, version)
    record = rig.store.extractions[(TENANT_A, version)]
    assert [b.text for b in result.extracted.blocks] == [SECRET, "Second paragraph."]
    assert (result.document_id, result.version_id) == (document, version)
    assert (result.parser_version, result.content_hash) == (
        record.parser_version,
        record.content_hash,
    )
    assert content_hash(result.extracted.to_bytes()) == result.content_hash


def test_the_text_never_appears_in_a_repr(rig: Rig) -> None:
    document, version = extracted(rig)
    assert SECRET not in repr(read(rig, document, version))


def test_a_ready_version_is_served(rig: Rig) -> None:
    document, version = extracted(rig)
    rig.mark(document, version, VersionStatus.READY)
    assert read(rig, document, version).version_id == version


@pytest.mark.parametrize("status", [VersionStatus.FAILED, VersionStatus.QUARANTINED])
def test_a_failed_or_quarantined_version_is_refused(rig: Rig, status: VersionStatus) -> None:
    document, version = extracted(rig)
    rig.mark(document, version, status)
    with pytest.raises(ConflictError) as excinfo:
        read(rig, document, version)
    assert excinfo.value.code == EXTRACTION_VERSION_NOT_ELIGIBLE


def test_a_tombstoned_document_is_refused_before_any_purge(rig: Rig) -> None:
    document, version = extracted(rig)
    rig.tombstone(document)
    assert rig.objects.derived_keys()  # the artifact still exists: only Documents' verdict stops it
    with pytest.raises(NotFoundError) as excinfo:
        read(rig, document, version)
    assert excinfo.value.code == "document_not_found"


def test_another_tenant_cannot_read_it(rig: Rig) -> None:
    document, version = extracted(rig)
    with pytest.raises(NotFoundError):
        read(rig, document, version, SystemContext(TENANT_B, "other"))


def test_unknown_and_foreign_versions_are_refused(rig: Rig) -> None:
    document, version = extracted(rig)
    other_document, other_version = extracted(rig)
    with pytest.raises(NotFoundError):
        read(rig, document, other_version)
    with pytest.raises(NotFoundError):
        read(rig, other_document, version)


def test_a_version_never_extracted_is_not_found(rig: Rig) -> None:
    document, version = rig.upload_document(BODY)
    with pytest.raises(NotFoundError) as excinfo:
        read(rig, document, version)
    assert excinfo.value.code == EXTRACTION_NOT_FOUND


def test_a_row_without_its_object_is_reported_missing(rig: Rig) -> None:
    document, version = extracted(rig)
    rig.objects.delete(rig.key(document, version))
    with pytest.raises(NotFoundError) as excinfo:
        read(rig, document, version)
    assert excinfo.value.code == EXTRACTION_ARTIFACT_MISSING


def test_a_substituted_artifact_is_refused(rig: Rig) -> None:
    document, version = extracted(rig)
    other_document, other_version = rig.upload_document(b"Different text entirely.")
    rig.stage.run(rig.request(other_document, other_version))
    rig.objects._objects[rig.key(document, version).render()] = rig.objects.get(  # noqa: SLF001
        rig.key(other_document, other_version)
    )
    with pytest.raises(ConflictError) as excinfo:
        read(rig, document, version)
    assert excinfo.value.code == EXTRACTION_ARTIFACT_CORRUPT


def test_an_artifact_that_is_not_a_valid_extraction_is_refused(rig: Rig) -> None:
    document, version = extracted(rig)
    garbage = b"not json"
    rig.objects._objects[rig.key(document, version).render()] = garbage  # noqa: SLF001
    key = (TENANT_A, version)
    rig.store.extractions[key] = dataclasses.replace(
        rig.store.extractions[key], content_hash=content_hash(garbage)
    )
    with pytest.raises(ConflictError) as excinfo:
        read(rig, document, version)
    assert excinfo.value.code == EXTRACTION_ARTIFACT_CORRUPT


def test_recorded_counts_that_disagree_with_the_artifact_are_refused(rig: Rig) -> None:
    document, version = extracted(rig)
    key = (TENANT_A, version)
    rig.store.extractions[key] = dataclasses.replace(rig.store.extractions[key], block_count=99)
    with pytest.raises(ConflictError) as excinfo:
        read(rig, document, version)
    assert excinfo.value.code == EXTRACTION_ARTIFACT_CORRUPT


def test_an_object_store_failure_is_not_classified_it_propagates(rig: Rig) -> None:
    document, version = extracted(rig)
    rig.objects.fail_get = True
    with pytest.raises(InfrastructureError):
        read(rig, document, version)
