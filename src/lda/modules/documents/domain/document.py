"""Document and DocumentVersion (ADR-015). Versions are immutable; a document only ever gains
versions and is tombstoned once, irreversibly (ADR-023)."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.shared.kernel.errors import ConflictError, ValidationError
from lda.shared.kernel.ids import PrincipalId, TenantId

MAX_TITLE_LENGTH = 255
MEDIA_TYPE_PATTERN = re.compile(r"[a-z0-9][a-z0-9.+-]{0,62}/[a-z0-9][a-z0-9.+-]{0,62}")


class DocumentStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"  # tombstoned: unreadable, immutable, awaiting purge


class VersionStatus(StrEnum):
    """`pending` is never searchable. A version leaves it exactly once, to a terminal outcome that
    ingestion reports and only Documents applies (ADR-015 amendment, ADR-010 §7)."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    QUARANTINED = "quarantined"


def clean_title(raw: str) -> str:
    title = raw.strip()
    if not 1 <= len(title) <= MAX_TITLE_LENGTH or not title.isprintable():
        raise ValidationError("document_title_invalid")
    return title


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    id: DocumentVersionId
    document_id: DocumentId
    tenant_id: TenantId
    number: int
    status: VersionStatus
    media_type: str
    size_bytes: int
    created_by: PrincipalId | None
    created_at: datetime

    def __post_init__(self) -> None:
        if self.number < 1:
            raise ValidationError("document_version_number_invalid")
        if self.size_bytes < 1:
            raise ValidationError("document_size_invalid")
        if MEDIA_TYPE_PATTERN.fullmatch(self.media_type) is None:
            raise ValidationError("document_media_type_invalid")
        if self.created_at.utcoffset() is None:
            raise ValidationError("document_time_naive")

    def with_outcome(self, outcome: VersionStatus) -> DocumentVersion:
        """`pending -> ready | failed | quarantined`, once. Terminal states never change."""
        if outcome is VersionStatus.PENDING or self.status is not VersionStatus.PENDING:
            raise ConflictError("document_version_outcome_invalid")
        return replace(self, status=outcome)


@dataclass(slots=True)
class Document:
    id: DocumentId
    tenant_id: TenantId
    title: str
    status: DocumentStatus
    created_by: PrincipalId | None
    created_at: datetime
    latest_version_number: int
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.latest_version_number < 1:
            raise ValidationError("document_version_number_invalid")
        if (self.status is DocumentStatus.DELETED) != (self.deleted_at is not None):
            raise ValidationError("document_status_invalid")

    @classmethod
    def create(
        cls,
        *,
        document_id: DocumentId,
        version_id: DocumentVersionId,
        tenant_id: TenantId,
        title: str,
        media_type: str,
        size_bytes: int,
        created_by: PrincipalId | None,
        now: datetime,
    ) -> tuple[Document, DocumentVersion]:
        document = cls(
            document_id, tenant_id, clean_title(title), DocumentStatus.ACTIVE, created_by, now, 1
        )
        version = DocumentVersion(
            version_id,
            document_id,
            tenant_id,
            1,
            VersionStatus.PENDING,
            media_type,
            size_bytes,
            created_by,
            now,
        )
        return document, version

    def add_version(
        self,
        *,
        version_id: DocumentVersionId,
        media_type: str,
        size_bytes: int,
        created_by: PrincipalId | None,
        now: datetime,
    ) -> DocumentVersion:
        if self.status is DocumentStatus.DELETED:
            raise ConflictError("document_deleted")
        version = DocumentVersion(
            version_id,
            self.id,
            self.tenant_id,
            self.latest_version_number + 1,
            VersionStatus.PENDING,
            media_type,
            size_bytes,
            created_by,
            now,
        )
        self.latest_version_number = version.number
        return version

    def tombstone(self, now: datetime) -> bool:
        """True if this call deleted the document; False if it already was (idempotent no-op)."""
        if self.status is DocumentStatus.DELETED:
            return False
        self.status = DocumentStatus.DELETED
        self.deleted_at = now
        return True
