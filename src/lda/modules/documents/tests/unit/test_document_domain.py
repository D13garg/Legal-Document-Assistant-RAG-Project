"""Document and version rules (ADR-015): immutability, numbering, one-way tombstone."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from lda.modules.documents.domain.document import (
    Document,
    DocumentStatus,
    DocumentVersion,
    VersionStatus,
    clean_title,
)
from lda.modules.documents.domain.ids import DocumentVersionId
from lda.modules.documents.domain.resource import DOCUMENT_ACTIONS, DOCUMENT_KIND, original_path
from lda.modules.documents.tests.support import ALICE, PDF, START, TENANT_A, a_document_id
from lda.shared.kernel.errors import ConflictError, ValidationError
from lda.shared.ports.object_store import TenantScopedKey

V1, V2 = DocumentVersionId(UUID(int=0x51)), DocumentVersionId(UUID(int=0x52))


def make(title: str = "MSA") -> tuple[Document, DocumentVersion]:
    return Document.create(
        document_id=a_document_id(),
        version_id=V1,
        tenant_id=TENANT_A,
        title=title,
        media_type=PDF,
        size_bytes=10,
        created_by=ALICE,
        now=START,
    )


def code_of(action: object) -> str:
    with pytest.raises(ValidationError) as caught:
        action()  # type: ignore[operator]
    return caught.value.code


def test_create_builds_an_active_document_with_a_pending_first_version() -> None:
    document, version = make()
    assert (document.status, document.latest_version_number, document.deleted_at) == (
        DocumentStatus.ACTIVE,
        1,
        None,
    )
    assert (version.number, version.status, version.document_id) == (
        1,
        VersionStatus.PENDING,
        document.id,
    )
    assert version.status is not VersionStatus.PENDING or version.status.value == "pending"


def test_titles_are_trimmed_and_bounded() -> None:
    assert clean_title("  Lease  ") == "Lease"
    assert clean_title("a" * 255) == "a" * 255
    for bad in ["", "   ", "a" * 256, "two\nlines", "tab\there", "nul\x00"]:
        assert code_of(lambda bad=bad: clean_title(bad)) == "document_title_invalid"


@pytest.mark.parametrize(
    "media_type", ["", "pdf", "APPLICATION/PDF", "application/", "/pdf", "a b/c", "a" * 64 + "/b"]
)
def test_bad_media_types_are_rejected(media_type: str) -> None:
    _, version = make()
    code = code_of(lambda: replace(version, media_type=media_type))
    assert code == "document_media_type_invalid"


@pytest.mark.parametrize("size", [0, -1])
def test_a_version_needs_at_least_one_byte(size: int) -> None:
    _, version = make()
    assert code_of(lambda: replace(version, size_bytes=size)) == "document_size_invalid"


def test_version_numbers_start_at_one_and_time_must_be_aware() -> None:
    _, version = make()
    assert code_of(lambda: replace(version, number=0)) == "document_version_number_invalid"
    assert (
        code_of(lambda: replace(version, created_at=datetime(2026, 1, 1))) == "document_time_naive"
    )


def test_a_version_is_immutable() -> None:
    _, version = make()
    with pytest.raises(FrozenInstanceError):
        version.number = 9  # type: ignore[misc]


def test_each_new_version_gets_the_next_number_and_is_pending() -> None:
    document, _ = make()
    second = document.add_version(
        version_id=V2,
        media_type=PDF,
        size_bytes=5,
        created_by=ALICE,
        now=START + timedelta(hours=1),
    )
    assert (second.number, document.latest_version_number, second.status) == (
        2,
        2,
        VersionStatus.PENDING,
    )


def test_a_tombstoned_document_accepts_no_version_and_keeps_its_counter() -> None:
    document, _ = make()
    assert document.tombstone(START) is True
    with pytest.raises(ConflictError) as caught:
        document.add_version(
            version_id=V2, media_type=PDF, size_bytes=5, created_by=None, now=START
        )
    assert caught.value.code == "document_deleted"
    assert document.latest_version_number == 1


def test_tombstone_is_one_way_and_idempotent() -> None:
    document, _ = make()
    later = START + timedelta(days=1)
    assert document.tombstone(START) is True
    assert document.tombstone(later) is False
    assert (document.status, document.deleted_at) == (DocumentStatus.DELETED, START)


def test_status_and_deleted_at_must_agree_and_counter_must_be_positive() -> None:
    document, _ = make()
    assert (
        code_of(lambda: replace(document, status=DocumentStatus.DELETED))
        == "document_status_invalid"
    )
    assert code_of(lambda: replace(document, deleted_at=START)) == "document_status_invalid"
    assert (
        code_of(lambda: replace(document, latest_version_number=0))
        == "document_version_number_invalid"
    )
    assert replace(document, status=DocumentStatus.DELETED, deleted_at=START).deleted_at == START


def test_created_by_may_be_absent_for_system_work() -> None:
    document, version = Document.create(
        document_id=a_document_id(),
        version_id=V1,
        tenant_id=TENANT_A,
        title="x",
        media_type=PDF,
        size_bytes=1,
        created_by=None,
        now=START,
    )
    assert document.created_by is None and version.created_by is None


def test_the_original_key_carries_identifiers_only() -> None:
    path = original_path(a_document_id(), V1)
    assert path == f"documents/{a_document_id()}/{V1}/original"
    key = TenantScopedKey(TENANT_A, path)
    assert key.render().startswith(f"tenant/{TENANT_A}/documents/")


def test_the_resource_vocabulary_is_the_approved_one() -> None:
    assert DOCUMENT_KIND == "document"
    assert DOCUMENT_ACTIONS == (
        "documents.read_document",
        "documents.delete_document",
        "documents.add_document_version",
    )
    assert datetime.now(UTC).tzinfo is not None
