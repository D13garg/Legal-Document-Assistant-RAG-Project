"""In-memory Documents adapters with the same observable behaviour as the SQL ones.

A unit of work works on copies and publishes them to the shared store only on `commit`, so rollback,
tenant isolation, the tombstone-only deletes and atomic revocation can be tested without a
database. A contract suite runs against both adapters.
"""

from __future__ import annotations

from dataclasses import replace
from types import TracebackType
from typing import Self

from lda.modules.documents.domain.document import (
    Document,
    DocumentStatus,
    DocumentVersion,
    VersionStatus,
)
from lda.modules.documents.domain.evidence import PurgeReceipt, UploadIntent
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import JobId, TenantId
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobSpec

ResourceKey = tuple[TenantId, str, str]


class InMemoryDocumentsStore:
    """The committed state, shared by every unit of work of one test."""

    def __init__(self) -> None:
        self.documents: dict[tuple[TenantId, DocumentId], Document] = {}
        self.versions: dict[tuple[TenantId, DocumentVersionId], DocumentVersion] = {}
        self.intents: dict[tuple[TenantId, DocumentVersionId], UploadIntent] = {}
        self.receipts: dict[tuple[TenantId, DocumentId], PurgeReceipt] = {}
        self.grants: dict[ResourceKey, int] = {}  # seed: grants the fake revoker will remove
        self.revocations: list[tuple[ResourceKey, int]] = []
        self.fail_revocation = False


class _Recorder:
    def __init__(self) -> None:
        self.facts: list[AuditFact] = []
        self.events: list[tuple[DomainEvent, Delivery]] = []
        self.jobs: list[JobSpec] = []

    def record(self, fact: AuditFact) -> None:
        self.facts.append(fact)

    def publish(self, event: DomainEvent, delivery: Delivery) -> None:
        self.events.append((event, delivery))

    def enqueue(self, spec: JobSpec) -> JobId | None:
        self.jobs.append(spec)
        return None


class _Bound:
    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def _check(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant_id:
            raise InfrastructureError("documents_tenant_mismatch")


class InMemoryDocumentRepository(_Bound):
    def __init__(self, tenant_id: TenantId, working: dict[tuple[TenantId, DocumentId], Document]):
        super().__init__(tenant_id)
        self._working = working

    def add(self, document: Document) -> None:
        self._check(document.tenant_id)
        key = (document.tenant_id, document.id)
        if key in self._working:
            raise InfrastructureError("documents_persistence_failed")
        self._working[key] = replace(document)

    def get(self, document_id: DocumentId, *, for_update: bool = False) -> Document | None:
        found = self._working.get((self._tenant_id, document_id))
        return None if found is None else replace(found)

    def save(self, document: Document) -> None:
        self._check(document.tenant_id)
        key = (document.tenant_id, document.id)
        current = self._working.get(key)
        if current is None:
            raise InfrastructureError("document_not_saved")
        if current.status is DocumentStatus.DELETED:
            raise InfrastructureError("documents_persistence_failed")  # a tombstone is immutable
        self._working[key] = replace(document)

    def remove(self, document_id: DocumentId) -> bool:
        key = (self._tenant_id, document_id)
        current = self._working.get(key)
        if current is None:
            return False
        if current.status is not DocumentStatus.DELETED:
            raise InfrastructureError("documents_persistence_failed")  # purge only
        del self._working[key]
        return True


class InMemoryVersionRepository(_Bound):
    def __init__(
        self,
        tenant_id: TenantId,
        working: dict[tuple[TenantId, DocumentVersionId], DocumentVersion],
        documents: dict[tuple[TenantId, DocumentId], Document],
    ) -> None:
        super().__init__(tenant_id)
        self._working = working
        self._documents = documents

    def add(self, version: DocumentVersion) -> None:
        self._check(version.tenant_id)
        key = (version.tenant_id, version.id)
        duplicate_number = any(
            v.document_id == version.document_id and v.number == version.number
            for (t, _), v in self._working.items()
            if t == self._tenant_id
        )
        owner = self._documents.get((self._tenant_id, version.document_id))
        if key in self._working or duplicate_number or owner is None:  # owner: the SQL foreign key
            raise InfrastructureError("documents_persistence_failed")
        self._working[key] = version

    def get(self, version_id: DocumentVersionId) -> DocumentVersion | None:
        return self._working.get((self._tenant_id, version_id))

    def list_for(self, document_id: DocumentId) -> tuple[DocumentVersion, ...]:
        found = [
            v
            for (t, _), v in self._working.items()
            if t == self._tenant_id and v.document_id == document_id
        ]
        return tuple(sorted(found, key=lambda v: v.number))

    def set_outcome(self, version_id: DocumentVersionId, outcome: VersionStatus) -> bool:
        key = (self._tenant_id, version_id)
        current = self._working.get(key)
        if current is None or current.status is not VersionStatus.PENDING:
            return False
        owner = self._documents.get((self._tenant_id, current.document_id))
        if outcome is VersionStatus.READY and (
            owner is None or owner.status is not DocumentStatus.ACTIVE
        ):
            raise InfrastructureError("documents_persistence_failed")  # the database trigger
        self._working[key] = replace(current, status=outcome)
        return True

    def remove_all_for(self, document_id: DocumentId) -> int:
        owner = self._documents.get((self._tenant_id, document_id))
        mine = [
            k
            for k, v in self._working.items()
            if k[0] == self._tenant_id and v.document_id == document_id
        ]
        if mine and (owner is None or owner.status is not DocumentStatus.DELETED):
            raise InfrastructureError("documents_persistence_failed")  # purge only
        for key in mine:
            del self._working[key]
        return len(mine)


class InMemoryUploadIntentRepository(_Bound):
    def __init__(
        self, tenant_id: TenantId, working: dict[tuple[TenantId, DocumentVersionId], UploadIntent]
    ) -> None:
        super().__init__(tenant_id)
        self._working = working

    def add(self, intent: UploadIntent) -> None:
        self._check(intent.tenant_id)
        key = (intent.tenant_id, intent.version_id)
        if key in self._working:
            raise InfrastructureError("documents_persistence_failed")
        self._working[key] = intent

    def get(
        self, version_id: DocumentVersionId, *, for_update: bool = False
    ) -> UploadIntent | None:
        return self._working.get((self._tenant_id, version_id))

    def remove(self, version_id: DocumentVersionId) -> bool:
        return self._working.pop((self._tenant_id, version_id), None) is not None


class InMemoryPurgeReceiptRepository(_Bound):
    def __init__(
        self, tenant_id: TenantId, working: dict[tuple[TenantId, DocumentId], PurgeReceipt]
    ) -> None:
        super().__init__(tenant_id)
        self._working = working

    def add(self, receipt: PurgeReceipt) -> None:
        self._check(receipt.tenant_id)
        key = (receipt.tenant_id, receipt.document_id)
        if key in self._working:
            raise InfrastructureError("documents_persistence_failed")
        self._working[key] = receipt

    def get(self, document_id: DocumentId) -> PurgeReceipt | None:
        return self._working.get((self._tenant_id, document_id))


class _StagedRevoker:
    """Revocation staged on the unit: it is applied by `commit` and dropped by a rollback."""

    def __init__(self, tenant_id: TenantId, store: InMemoryDocumentsStore) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self.staged: list[tuple[ResourceKey, int]] = []

    def revoke_all(self, resource_kind: str, resource_id: str) -> int:
        if self._store.fail_revocation:
            raise InfrastructureError("access_control_persistence_failed")
        key = (self._tenant_id, resource_kind, resource_id)
        already = sum(n for k, n in self.staged if k == key)
        count = max(self._store.grants.get(key, 0) - already, 0)
        self.staged.append((key, count))
        return count


class InMemoryDocumentsUnitOfWork:
    def __init__(self, tenant_id: TenantId, store: InMemoryDocumentsStore) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self._documents = {k: replace(v) for k, v in store.documents.items()}
        self._versions = dict(store.versions)
        self._intents = dict(store.intents)
        self._receipts = dict(store.receipts)
        self._shared = _Recorder()
        self._revoker = _StagedRevoker(tenant_id, store)
        self.documents_repo = InMemoryDocumentRepository(tenant_id, self._documents)
        self.versions_repo = InMemoryVersionRepository(tenant_id, self._versions, self._documents)
        self.intents_repo = InMemoryUploadIntentRepository(tenant_id, self._intents)
        self.receipts_repo = InMemoryPurgeReceiptRepository(tenant_id, self._receipts)
        self.committed = False
        self.rolled_back = False

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    @property
    def events(self) -> _Recorder:
        return self._shared

    @property
    def jobs(self) -> _Recorder:
        return self._shared

    @property
    def audit(self) -> _Recorder:
        return self._shared

    @property
    def documents(self) -> InMemoryDocumentRepository:
        return self.documents_repo

    @property
    def versions(self) -> InMemoryVersionRepository:
        return self.versions_repo

    @property
    def intents(self) -> InMemoryUploadIntentRepository:
        return self.intents_repo

    @property
    def receipts(self) -> InMemoryPurgeReceiptRepository:
        return self.receipts_repo

    @property
    def access_revoker(self) -> _StagedRevoker:
        return self._revoker

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.committed:
            self.rolled_back = True

    def commit(self) -> None:
        self._store.documents = self._documents
        self._store.versions = self._versions
        self._store.intents = self._intents
        self._store.receipts = self._receipts
        for key, count in self._revoker.staged:
            self._store.grants[key] = max(self._store.grants.get(key, 0) - count, 0)
            self._store.revocations.append((key, count))
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class InMemoryDocumentsUnitOfWorkFactory:
    def __init__(self, store: InMemoryDocumentsStore | None = None) -> None:
        self.store = store or InMemoryDocumentsStore()
        self.begun: list[InMemoryDocumentsUnitOfWork] = []

    def begin(self, ctx: ExecutionContext) -> InMemoryDocumentsUnitOfWork:
        uow = InMemoryDocumentsUnitOfWork(ctx.tenant_id, self.store)
        self.begun.append(uow)
        return uow
