"""Shared rig: Documents (in-memory, via its public API and fakes) + document_processing."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from lda.modules.document_processing.api import (
    ExtractionLimits,
    ExtractStage,
    PurgeExtractions,
    ReadExtraction,
)
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.domain.resource import extracted_path
from lda.modules.document_processing.infrastructure.parsers.registry import FormatParsers
from lda.modules.document_processing.testing import (
    InMemoryProcessingStore,
    InMemoryProcessingUnitOfWorkFactory,
)
from lda.modules.documents.api import (
    DeleteDocument,
    DocumentReference,
    ReadOriginal,
    ReadVersionState,
    RecordOutcomeCommand,
    RecordVersionOutcome,
    UploadDocument,
    UploadDocumentCommand,
    UploadPolicy,
    UploadStaging,
    VersionStatus,
)
from lda.modules.documents.api import DocumentId as SourceDocumentId
from lda.modules.documents.api import DocumentVersionId as SourceVersionId
from lda.modules.documents.testing import (
    InMemoryDocumentsStore,
    InMemoryDocumentsUnitOfWorkFactory,
)
from lda.modules.ingestion.api import DocumentRef, IngestionRunId, StageRequest, VersionRef
from lda.platform.testing.doubles import FakeClock, InMemoryObjectStore, SequentialIdGenerator
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import JobId, PrincipalId, RequestId, TenantId
from lda.shared.ports.object_store import StoredObject, TenantScopedKey

TENANT_A = TenantId(UUID(int=0xA1))
TENANT_B = TenantId(UUID(int=0xB2))
ALICE = PrincipalId(UUID(int=0xA11CE))
START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
PDF = "application/pdf"
TEXT = "text/plain"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CSV = "text/csv"
LIMITS = ExtractionLimits(
    max_bytes=100_000, max_pages=5, max_characters=1000, max_expanded_bytes=100_000
)
POLICY = UploadPolicy(
    max_bytes=100_000, allowed_media_types=frozenset({PDF, TEXT, DOCX, CSV}), grace_seconds=900
)
SECRET = "Confidential indemnity clause"  # noqa: S105
SYSTEM_A = SystemContext(TENANT_A, "document-processing-test")


def ctx(tenant: TenantId = TENANT_A, n: int = 1) -> TenantContext:
    return TenantContext(tenant, ALICE, RequestId(UUID(int=n)))


class FlakyObjectStore(InMemoryObjectStore):
    """An object store that can be told to fail, to lie about deletes or to act mid-operation."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_put = False
        self.fail_get = False
        self.fail_delete = False
        self.ignore_delete = False
        self.after_put: Callable[[], None] | None = None
        self.after_delete: Callable[[], None] | None = None
        self.put_calls: list[tuple[str, bool]] = []

    def derived_keys(self) -> list[str]:
        """Rendered keys of every derived artifact currently stored."""
        return [k for k in self._objects if "/document_processing/" in k]

    def remove_originals(self) -> None:
        """Delete every original (Documents' own objects), as a lost or corrupted bucket would."""
        for key in [k for k in self._objects if k.endswith("/original")]:
            del self._objects[key]

    def replace_originals(self, body: bytes) -> None:
        for key in [k for k in self._objects if k.endswith("/original")]:
            self._objects[key] = body

    def put(
        self, key: TenantScopedKey, body: bytes, *, content_type: str, overwrite: bool = False
    ) -> StoredObject:
        if self.fail_put and "document_processing/" in key.render():
            raise InfrastructureError("object_store_unavailable")
        self.put_calls.append((key.render(), overwrite))
        stored = super().put(key, body, content_type=content_type, overwrite=overwrite)
        if self.after_put is not None:
            self.after_put()
        return stored

    def get(self, key: TenantScopedKey) -> bytes:
        if self.fail_get:
            raise InfrastructureError("object_store_unavailable")
        return super().get(key)

    def delete(self, key: TenantScopedKey) -> None:
        if self.fail_delete:
            raise InfrastructureError("object_store_unavailable")
        if not self.ignore_delete:
            super().delete(key)
        if self.after_delete is not None:
            self.after_delete()


@dataclass
class Rig:
    clock: FakeClock
    objects: FlakyObjectStore
    documents: InMemoryDocumentsUnitOfWorkFactory
    processing: InMemoryProcessingUnitOfWorkFactory
    upload: UploadDocument
    delete: DeleteDocument
    record_outcome: RecordVersionOutcome
    reader: ReadOriginal
    stage: ExtractStage
    purge: PurgeExtractions
    read_extraction: ReadExtraction
    limits: ExtractionLimits

    @property
    def store(self) -> InMemoryProcessingStore:
        return self.processing.store

    def upload_document(
        self, body: bytes, media_type: str = TEXT, tenant: TenantId = TENANT_A
    ) -> tuple[DocumentId, VersionId]:
        view = self.upload.execute(
            ctx(tenant), UploadDocumentCommand(title="Agreement", media_type=media_type, body=body)
        )
        return DocumentId(view.document_id.value), VersionId(view.version_id.value)

    def request(
        self,
        document: DocumentId,
        version: VersionId,
        tenant: TenantId = TENANT_A,
        token: int = 1,
    ) -> StageRequest:
        return StageRequest(
            tenant,
            DocumentRef(document.value),
            VersionRef(version.value),
            IngestionRunId(UUID(int=0x4001)),
            "extract",
            1,
            JobId(UUID(int=0x7001)),
            token,
        )

    def key(
        self, document: DocumentId, version: VersionId, tenant: TenantId = TENANT_A
    ) -> TenantScopedKey:

        return TenantScopedKey(tenant, extracted_path(document, version))

    def make_stage(
        self, parser: FormatParsers, limits: ExtractionLimits | None = None
    ) -> ExtractStage:
        return ExtractStage(
            self.reader, parser, self.processing, self.objects, self.clock, limits or self.limits
        )

    def mark(self, document: DocumentId, version: VersionId, status: VersionStatus) -> None:
        self.record_outcome.execute(
            SYSTEM_A,
            RecordOutcomeCommand(
                SourceDocumentId(document.value), SourceVersionId(version.value), status
            ),
        )

    def tombstone(self, document: DocumentId) -> None:
        self.delete.execute(ctx(), DocumentReference(SourceDocumentId(document.value)))


def build_rig(limits: ExtractionLimits = LIMITS) -> Rig:
    clock = FakeClock(START)
    ids = SequentialIdGenerator()
    objects = FlakyObjectStore()
    documents = InMemoryDocumentsUnitOfWorkFactory(InMemoryDocumentsStore())
    processing = InMemoryProcessingUnitOfWorkFactory()
    staging = UploadStaging(documents, objects, clock, ids, POLICY)
    reader = ReadOriginal(documents, objects)
    rig = Rig(
        clock=clock,
        objects=objects,
        documents=documents,
        processing=processing,
        upload=UploadDocument(documents, staging, clock, ids),
        delete=DeleteDocument(documents, clock, ids),
        record_outcome=RecordVersionOutcome(documents, clock),
        reader=reader,
        stage=ExtractStage(reader, FormatParsers(), processing, objects, clock, limits),
        purge=PurgeExtractions(processing, objects, clock),
        read_extraction=ReadExtraction(processing, objects, ReadVersionState(documents)),
        limits=limits,
    )
    return rig
