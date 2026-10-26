"""`ReadOriginal`: the only content read path, for pipeline work on a still-pending version."""

from __future__ import annotations

from uuid import UUID

import pytest

from lda.modules.documents.application.dto.documents import (
    DocumentReference,
    ReadOriginalCommand,
    RecordOutcomeCommand,
)
from lda.modules.documents.application.queries.read_original import (
    DOCUMENT_NOT_FOUND,
    ORIGINAL_MISSING,
    ORIGINAL_SIZE_MISMATCH,
    VERSION_NOT_FOUND,
    VERSION_NOT_PENDING,
    ReadOriginal,
)
from lda.modules.documents.application.use_cases.record_version_outcome import (
    RecordVersionOutcome,
)
from lda.modules.documents.domain.document import VersionStatus
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.documents.domain.resource import original_path
from lda.modules.documents.tests.support import (
    BODY,
    PDF,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    Rig,
    ctx_for,
    upload_command,
)
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError, NotFoundError
from lda.shared.ports.object_store import TenantScopedKey


@pytest.fixture
def reader(rig: Rig) -> ReadOriginal:
    return ReadOriginal(rig.factory, rig.store)


def _uploaded(rig: Rig) -> tuple[DocumentId, DocumentVersionId]:
    view = rig.upload.execute(ctx_for(TENANT_A), upload_command())
    return view.document_id, view.version_id


def test_returns_bytes_and_recorded_metadata(rig: Rig, reader: ReadOriginal) -> None:
    document_id, version_id = _uploaded(rig)
    content = reader.execute(SYSTEM_A, ReadOriginalCommand(document_id, version_id))
    assert content.body == BODY
    assert (content.media_type, content.size_bytes) == (PDF, len(BODY))
    assert (content.document_id, content.version_id) == (document_id, version_id)


def test_content_never_appears_in_a_repr(rig: Rig, reader: ReadOriginal) -> None:
    document_id, version_id = _uploaded(rig)
    content = reader.execute(SYSTEM_A, ReadOriginalCommand(document_id, version_id))
    assert "confidential" not in repr(content)


def test_other_tenant_cannot_read(rig: Rig, reader: ReadOriginal) -> None:
    document_id, version_id = _uploaded(rig)
    other = SystemContext(TENANT_B, "documents-test")
    with pytest.raises(NotFoundError) as excinfo:
        reader.execute(other, ReadOriginalCommand(document_id, version_id))
    assert excinfo.value.code == DOCUMENT_NOT_FOUND


def test_unknown_document_and_unknown_version(rig: Rig, reader: ReadOriginal) -> None:
    document_id, version_id = _uploaded(rig)
    with pytest.raises(NotFoundError) as unknown_document:
        reader.execute(SYSTEM_A, ReadOriginalCommand(DocumentId(UUID(int=999)), version_id))
    assert unknown_document.value.code == DOCUMENT_NOT_FOUND
    with pytest.raises(NotFoundError) as unknown_version:
        reader.execute(SYSTEM_A, ReadOriginalCommand(document_id, DocumentVersionId(UUID(int=999))))
    assert unknown_version.value.code == VERSION_NOT_FOUND


def test_version_of_another_document_is_refused(rig: Rig, reader: ReadOriginal) -> None:
    first_document, _ = _uploaded(rig)
    _, other_version = _uploaded(rig)
    with pytest.raises(NotFoundError) as excinfo:
        reader.execute(SYSTEM_A, ReadOriginalCommand(first_document, other_version))
    assert excinfo.value.code == VERSION_NOT_FOUND


@pytest.mark.parametrize("outcome", [VersionStatus.READY, VersionStatus.FAILED])
def test_a_version_that_left_pending_is_refused(
    rig: Rig, reader: ReadOriginal, outcome: VersionStatus
) -> None:
    document_id, version_id = _uploaded(rig)
    RecordVersionOutcome(rig.factory, rig.clock).execute(
        SYSTEM_A, RecordOutcomeCommand(document_id, version_id, outcome)
    )
    with pytest.raises(ConflictError) as excinfo:
        reader.execute(SYSTEM_A, ReadOriginalCommand(document_id, version_id))
    assert excinfo.value.code == VERSION_NOT_PENDING


def test_a_tombstoned_document_is_refused(rig: Rig, reader: ReadOriginal) -> None:
    document_id, version_id = _uploaded(rig)
    rig.delete.execute(ctx_for(TENANT_A), DocumentReference(document_id))
    with pytest.raises(NotFoundError) as excinfo:
        reader.execute(SYSTEM_A, ReadOriginalCommand(document_id, version_id))
    assert excinfo.value.code == DOCUMENT_NOT_FOUND


def test_missing_object_is_reported_as_missing_original(rig: Rig, reader: ReadOriginal) -> None:
    document_id, version_id = _uploaded(rig)
    rig.store.delete(TenantScopedKey(TENANT_A, original_path(document_id, version_id)))
    with pytest.raises(NotFoundError) as excinfo:
        reader.execute(SYSTEM_A, ReadOriginalCommand(document_id, version_id))
    assert excinfo.value.code == ORIGINAL_MISSING


def test_a_substituted_object_is_refused(rig: Rig, reader: ReadOriginal) -> None:
    document_id, version_id = _uploaded(rig)
    key = TenantScopedKey(TENANT_A, original_path(document_id, version_id))
    rig.store.put(key, b"%PDF-1.7 short", content_type=PDF, overwrite=True)
    with pytest.raises(InfrastructureError) as excinfo:
        reader.execute(SYSTEM_A, ReadOriginalCommand(document_id, version_id))
    assert excinfo.value.code == ORIGINAL_SIZE_MISMATCH
