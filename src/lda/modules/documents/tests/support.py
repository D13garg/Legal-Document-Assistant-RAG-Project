from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from lda.modules.documents.application.dto.documents import UploadDocumentCommand
from lda.modules.documents.application.policy import UploadPolicy
from lda.modules.documents.application.queries.get_document import GetDocument
from lda.modules.documents.application.queries.verify_document_purge import VerifyDocumentPurge
from lda.modules.documents.application.upload_staging import UploadStaging
from lda.modules.documents.application.use_cases.add_document_version import AddDocumentVersion
from lda.modules.documents.application.use_cases.delete_document import DeleteDocument
from lda.modules.documents.application.use_cases.purge_document import PurgeDocument
from lda.modules.documents.application.use_cases.reap_abandoned_upload import ReapAbandonedUpload
from lda.modules.documents.application.use_cases.upload_document import UploadDocument
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.documents.testing.in_memory import (
    InMemoryDocumentsUnitOfWork,
    InMemoryDocumentsUnitOfWorkFactory,
)
from lda.platform.testing.doubles import FakeClock, InMemoryObjectStore, SequentialIdGenerator
from lda.shared.kernel.context import ExecutionContext, SystemContext, TenantContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId

TENANT_A = TenantId(UUID(int=0xA1))
TENANT_B = TenantId(UUID(int=0xB2))
ALICE = PrincipalId(UUID(int=0xA11CE))
START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
PDF = "application/pdf"
POLICY = UploadPolicy(
    max_bytes=1000, allowed_media_types=frozenset({PDF, "text/plain"}), grace_seconds=900
)
BODY = b"%PDF-1.7 confidential clause text"
SYSTEM_A = SystemContext(TENANT_A, "documents-test")


def ctx_for(tenant: TenantId, principal: PrincipalId = ALICE, n: int = 1) -> TenantContext:
    return TenantContext(tenant, principal, RequestId(UUID(int=n)))


def upload_command(
    title: str = "Master services agreement", body: bytes = BODY
) -> UploadDocumentCommand:
    return UploadDocumentCommand(title=title, media_type=PDF, body=body)


@dataclass
class Rig:
    factory: InMemoryDocumentsUnitOfWorkFactory
    store: InMemoryObjectStore
    clock: FakeClock
    ids: SequentialIdGenerator
    staging: UploadStaging
    upload: UploadDocument
    add_version: AddDocumentVersion
    delete: DeleteDocument
    purge: PurgeDocument
    reap: ReapAbandonedUpload
    get: GetDocument
    verify: VerifyDocumentPurge


def build_rig(
    factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    store: InMemoryObjectStore | None = None,
    policy: UploadPolicy = POLICY,
) -> Rig:
    objects = store or InMemoryObjectStore()
    staging = UploadStaging(factory, objects, clock, ids, policy)
    return Rig(
        factory,
        objects,
        clock,
        ids,
        staging,
        UploadDocument(factory, staging, clock, ids),
        AddDocumentVersion(factory, staging, clock, ids),
        DeleteDocument(factory, clock, ids),
        PurgeDocument(factory, objects, clock, ids),
        ReapAbandonedUpload(factory, objects, clock, policy),
        GetDocument(factory),
        VerifyDocumentPurge(factory, objects),
    )


def a_document_id(n: int = 1) -> DocumentId:
    return DocumentId(UUID(int=0xD0C0 + n))


class FlakyStore(InMemoryObjectStore):
    """An object store that can be told to fail, to lie about deletes or to act mid-upload."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_put = False
        self.fail_delete = False
        self.ignore_delete = False
        self.after_put: Callable[[], None] | None = None
        self.after_delete: Callable[[], None] | None = None
        self.put_overwrite_flags: list[bool] = []

    def put(  # type: ignore[no-untyped-def]
        self, key, body, *, content_type, overwrite=False
    ):
        if self.fail_put:
            raise InfrastructureError("object_store_unavailable")
        self.put_overwrite_flags.append(overwrite)
        stored = super().put(key, body, content_type=content_type, overwrite=overwrite)
        if self.after_put is not None:
            self.after_put()
        return stored

    def delete(self, key) -> None:  # type: ignore[no-untyped-def]
        if self.fail_delete:
            raise InfrastructureError("object_store_unavailable")
        if not self.ignore_delete:
            super().delete(key)
        if self.after_delete is not None:
            self.after_delete()


class FaultyFactory(InMemoryDocumentsUnitOfWorkFactory):
    """Shares `inner`'s store and history; the commit of the n-th unit of work (0-based) fails,
    like a lost connection."""

    def __init__(self, inner: InMemoryDocumentsUnitOfWorkFactory, fail_commit_on: int) -> None:
        super().__init__(inner.store)
        self.begun = inner.begun
        self._fail_on = fail_commit_on

    def begin(self, ctx: ExecutionContext) -> InMemoryDocumentsUnitOfWork:
        uow = super().begin(ctx)
        if len(self.begun) - 1 == self._fail_on:

            def broken() -> None:
                raise InfrastructureError("database_unavailable")

            uow.commit = broken  # type: ignore[method-assign]
        return uow


def uploaded_ids(
    rig: Rig, ctx: ExecutionContext, **over: object
) -> tuple[DocumentId, DocumentVersionId]:
    view = rig.upload.execute(ctx, upload_command(**over))  # type: ignore[arg-type]
    return view.document_id, view.version_id


def all_recorded(factory: InMemoryDocumentsUnitOfWorkFactory) -> list[object]:
    """Every audit fact, event and job spec any unit of work recorded."""
    out: list[object] = []
    for uow in factory.begun:
        out += [*uow.audit.facts, *uow.events.events, *uow.jobs.jobs]
    return out
