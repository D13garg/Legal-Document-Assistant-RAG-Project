"""`ReadVersionState`: the pipeline's status check, with no content and no key (ADR-038)."""

from __future__ import annotations

from uuid import UUID

import pytest

from lda.modules.documents.application.dto.documents import (
    AddDocumentVersionCommand,
    DocumentReference,
    RecordOutcomeCommand,
    VersionStateQuery,
)
from lda.modules.documents.application.queries.read_original import (
    DOCUMENT_NOT_FOUND,
    VERSION_NOT_FOUND,
)
from lda.modules.documents.application.queries.read_version_state import ReadVersionState
from lda.modules.documents.application.use_cases.record_version_outcome import (
    RecordVersionOutcome,
)
from lda.modules.documents.domain.document import VersionStatus
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.documents.tests.support import (
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    Rig,
    ctx_for,
    upload_command,
)
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import NotFoundError


@pytest.fixture
def reader(rig: Rig) -> ReadVersionState:
    return ReadVersionState(rig.factory)


def _uploaded(rig: Rig) -> tuple[DocumentId, DocumentVersionId]:
    view = rig.upload.execute(ctx_for(TENANT_A), upload_command())
    return view.document_id, view.version_id


def test_a_new_version_is_pending(rig: Rig, reader: ReadVersionState) -> None:
    document_id, version_id = _uploaded(rig)
    view = reader.execute(SYSTEM_A, VersionStateQuery(document_id, version_id))
    assert (view.document_id, view.version_id, view.status) == (
        document_id,
        version_id,
        VersionStatus.PENDING,
    )


@pytest.mark.parametrize(
    "outcome", [VersionStatus.READY, VersionStatus.FAILED, VersionStatus.QUARANTINED]
)
def test_the_status_is_reported_whatever_it_is(
    rig: Rig, reader: ReadVersionState, outcome: VersionStatus
) -> None:
    document_id, version_id = _uploaded(rig)
    RecordVersionOutcome(rig.factory, rig.clock).execute(
        SYSTEM_A, RecordOutcomeCommand(document_id, version_id, outcome)
    )
    assert reader.execute(SYSTEM_A, VersionStateQuery(document_id, version_id)).status is outcome


def test_other_tenant_sees_nothing(rig: Rig, reader: ReadVersionState) -> None:
    document_id, version_id = _uploaded(rig)
    other = SystemContext(TENANT_B, "documents-test")
    with pytest.raises(NotFoundError) as excinfo:
        reader.execute(other, VersionStateQuery(document_id, version_id))
    assert excinfo.value.code == DOCUMENT_NOT_FOUND


def test_unknown_document_unknown_version_and_foreign_version(
    rig: Rig, reader: ReadVersionState
) -> None:
    first_document, first_version = _uploaded(rig)
    _, other_version = _uploaded(rig)
    with pytest.raises(NotFoundError) as unknown_document:
        reader.execute(SYSTEM_A, VersionStateQuery(DocumentId(UUID(int=999)), first_version))
    assert unknown_document.value.code == DOCUMENT_NOT_FOUND
    with pytest.raises(NotFoundError) as unknown_version:
        reader.execute(
            SYSTEM_A, VersionStateQuery(first_document, DocumentVersionId(UUID(int=999)))
        )
    assert unknown_version.value.code == VERSION_NOT_FOUND
    with pytest.raises(NotFoundError) as foreign:
        reader.execute(SYSTEM_A, VersionStateQuery(first_document, other_version))
    assert foreign.value.code == VERSION_NOT_FOUND


def test_a_tombstoned_document_is_gone(rig: Rig, reader: ReadVersionState) -> None:
    document_id, version_id = _uploaded(rig)
    rig.delete.execute(ctx_for(TENANT_A), DocumentReference(document_id))
    with pytest.raises(NotFoundError) as excinfo:
        reader.execute(SYSTEM_A, VersionStateQuery(document_id, version_id))
    assert excinfo.value.code == DOCUMENT_NOT_FOUND


def test_the_view_carries_the_documents_own_version_number(
    rig: Rig, reader: ReadVersionState
) -> None:
    """Step 11 (ADR-008): Documents stays the only source of version ordering."""
    document_id, first = _uploaded(rig)
    second = rig.add_version.execute(
        ctx_for(TENANT_A), AddDocumentVersionCommand(document_id, "text/plain", b"v2")
    ).version_id
    assert reader.execute(SYSTEM_A, VersionStateQuery(document_id, first)).number == 1
    assert reader.execute(SYSTEM_A, VersionStateQuery(document_id, second)).number == 2
