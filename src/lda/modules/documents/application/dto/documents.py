from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from lda.modules.documents.domain.document import Document, DocumentVersion, VersionStatus
from lda.modules.documents.domain.evidence import PURGE_SCOPE
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId


@dataclass(frozen=True, slots=True)
class UploadDocumentCommand:
    title: str
    media_type: str
    body: bytes = field(repr=False)  # content: never in a repr, a log or an audit fact


@dataclass(frozen=True, slots=True)
class AddDocumentVersionCommand:
    document_id: DocumentId
    media_type: str
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class DocumentReference:
    document_id: DocumentId


@dataclass(frozen=True, slots=True)
class ReadOriginalCommand:
    document_id: DocumentId
    version_id: DocumentVersionId


@dataclass(frozen=True, slots=True)
class VersionStateQuery:
    document_id: DocumentId
    version_id: DocumentVersionId


@dataclass(frozen=True, slots=True)
class VersionStateView:
    """Where one version stands in its lifecycle: identifiers, a status and the version's number
    (its position in the document's history), no key, no bytes. The number is Documents' own
    ordering fact (ADR-015); a reader that must pick the newest version asks here (ADR-008)."""

    document_id: DocumentId
    version_id: DocumentVersionId
    status: VersionStatus
    number: int


@dataclass(frozen=True, slots=True)
class OriginalContent:
    """One version's original bytes. Content: never in a repr, a log, an event or an audit fact."""

    document_id: DocumentId
    version_id: DocumentVersionId
    media_type: str
    size_bytes: int
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReapUploadCommand:
    document_id: DocumentId
    version_id: DocumentVersionId


@dataclass(frozen=True, slots=True)
class UploadedView:
    document_id: DocumentId
    version_id: DocumentVersionId
    version_number: int
    status: str


@dataclass(frozen=True, slots=True)
class VersionView:
    version_id: DocumentVersionId
    number: int
    status: str
    media_type: str
    size_bytes: int
    created_at: datetime

    @classmethod
    def of(cls, version: DocumentVersion) -> VersionView:
        return cls(
            version.id,
            version.number,
            version.status.value,
            version.media_type,
            version.size_bytes,
            version.created_at,
        )


@dataclass(frozen=True, slots=True)
class DocumentView:
    document_id: DocumentId
    title: str
    latest_version_number: int
    created_at: datetime
    versions: tuple[VersionView, ...]

    @classmethod
    def of(cls, document: Document, versions: tuple[DocumentVersion, ...]) -> DocumentView:
        return cls(
            document.id,
            document.title,
            document.latest_version_number,
            document.created_at,
            tuple(VersionView.of(v) for v in versions),
        )


@dataclass(frozen=True, slots=True)
class DeleteView:
    document_id: DocumentId
    changed: bool  # False: already deleted (a no-op that emits nothing)
    revoked_grants: int


@dataclass(frozen=True, slots=True)
class PurgeView:
    document_id: DocumentId
    purged: bool  # False: nothing to do (already purged or unknown)
    versions_purged: int


@dataclass(frozen=True, slots=True)
class ReapView:
    outcome: str  # orphan_removed | version_exists | already_clean | not_due
    retry_after_seconds: int = 0


@dataclass(frozen=True, slots=True)
class PurgeVerificationView:
    """What can be checked about a purge, and only about Documents' own data."""

    document_id: DocumentId
    receipt_found: bool
    rows_absent: bool
    originals_absent: bool
    scope: str = PURGE_SCOPE

    @property
    def verified(self) -> bool:
        return self.receipt_found and self.rows_absent and self.originals_absent


class OutcomeResult(StrEnum):
    APPLIED = "applied"  # pending -> outcome happened in this call
    ALREADY = "already"  # the version already has exactly this outcome
    REFUSED = "refused"  # the version has another terminal outcome: never changed
    DELETED = "deleted"  # the document is tombstoned: nothing is applied
    MISSING = "missing"  # no such version of that document in this tenant


@dataclass(frozen=True, slots=True)
class RecordOutcomeCommand:
    document_id: DocumentId
    version_id: DocumentVersionId
    outcome: VersionStatus


@dataclass(frozen=True, slots=True)
class OutcomeView:
    result: OutcomeResult
