"""Ports Documents' use cases need. Adapters: infrastructure (SQL) and testing (in-memory)."""

from __future__ import annotations

from typing import Protocol

from lda.modules.documents.domain.document import Document, DocumentVersion, VersionStatus
from lda.modules.documents.domain.evidence import PurgeReceipt, UploadIntent
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.shared.kernel.context import ExecutionContext
from lda.shared.ports.access_revoker import ResourceAccessRevoker
from lda.shared.ports.unit_of_work import UnitOfWork


class DocumentRepository(Protocol):
    """Bound to one tenant for its whole life."""

    def add(self, document: Document) -> None: ...

    def get(self, document_id: DocumentId, *, for_update: bool = False) -> Document | None:
        """`for_update` takes the row lock that serializes versions, tombstone and purge."""
        ...

    def save(self, document: Document) -> None:
        """Persist the only mutable fields: status, deleted_at, latest_version_number."""
        ...

    def remove(self, document_id: DocumentId) -> bool:
        """Physically delete a TOMBSTONED document row (purge only; the database refuses others)."""
        ...


class VersionRepository(Protocol):
    def add(self, version: DocumentVersion) -> None: ...

    def get(self, version_id: DocumentVersionId) -> DocumentVersion | None: ...

    def list_for(self, document_id: DocumentId) -> tuple[DocumentVersion, ...]:
        """Ordered by version number."""
        ...

    def set_outcome(self, version_id: DocumentVersionId, outcome: VersionStatus) -> bool:
        """Compare-and-set `pending -> outcome`; False when the version was not `pending`."""
        ...

    def remove_all_for(self, document_id: DocumentId) -> int:
        """Purge only: the database refuses unless the document is tombstoned."""
        ...


class UploadIntentRepository(Protocol):
    def add(self, intent: UploadIntent) -> None: ...

    def get(
        self, version_id: DocumentVersionId, *, for_update: bool = False
    ) -> UploadIntent | None: ...

    def remove(self, version_id: DocumentVersionId) -> bool: ...


class PurgeReceiptRepository(Protocol):
    def add(self, receipt: PurgeReceipt) -> None: ...

    def get(self, document_id: DocumentId) -> PurgeReceipt | None: ...


class DocumentsUnitOfWork(UnitOfWork, Protocol):
    @property
    def documents(self) -> DocumentRepository: ...

    @property
    def versions(self) -> VersionRepository: ...

    @property
    def intents(self) -> UploadIntentRepository: ...

    @property
    def receipts(self) -> PurgeReceiptRepository: ...

    @property
    def access_revoker(self) -> ResourceAccessRevoker:
        """Revokes on THIS unit's transaction, so tombstone and revocation commit together."""
        ...


class DocumentsUnitOfWorkFactory(Protocol):
    def begin(self, ctx: ExecutionContext) -> DocumentsUnitOfWork: ...
