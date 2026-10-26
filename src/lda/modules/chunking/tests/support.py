"""Shared rig: Documents + document_processing (in-memory, via public APIs and fakes) + chunking.

Extraction uses a tiny test parser (`# ` heading, `## ` subheading, blank-line paragraphs) so
chunking tests control the structure exactly without PDF or DOCX builders.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from lda.modules.chunking.api import ChunkingParameters, ChunkStage, PurgeChunks
from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.resource import chunks_path
from lda.modules.chunking.testing import InMemoryChunkingStore, InMemoryChunkingUnitOfWorkFactory
from lda.modules.document_processing.api import (
    Block,
    BlockKind,
    ExtractedDocument,
    ExtractionLimits,
    ExtractStage,
    ReadExtraction,
)
from lda.modules.document_processing.testing import InMemoryProcessingUnitOfWorkFactory
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
TEXT = "text/plain"
PARAMS = ChunkingParameters(max_characters=200, min_characters=40, max_chunks=1000)
LIMITS = ExtractionLimits(
    max_bytes=100_000, max_pages=5, max_characters=100_000, max_expanded_bytes=100_000
)
POLICY = UploadPolicy(max_bytes=100_000, allowed_media_types=frozenset({TEXT}), grace_seconds=900)
SECRET = "Confidential indemnity clause"  # noqa: S105
SYSTEM_A = SystemContext(TENANT_A, "chunking-test")


def ctx(tenant: TenantId = TENANT_A, n: int = 1) -> TenantContext:
    return TenantContext(tenant, ALICE, RequestId(UUID(int=n)))


class MarkdownishParser:
    """Test-only `ContentParser`: `# ` and `## ` start headings, blank lines separate paragraphs."""

    version = "test-parser-1"

    def parse(self, media_type: str, content: bytes, limits: ExtractionLimits) -> ExtractedDocument:
        blocks: list[Block] = []
        for raw in content.decode("utf-8").split("\n\n"):
            text = raw.strip()
            if text.startswith("## "):
                blocks.append(Block(BlockKind.HEADING, text[3:], level=2))
            elif text.startswith("# "):
                blocks.append(Block(BlockKind.HEADING, text[2:], level=1))
            elif text:
                blocks.append(Block(BlockKind.PARAGRAPH, " ".join(text.split())))
        return ExtractedDocument(media_type, tuple(blocks))


class FlakyObjectStore(InMemoryObjectStore):
    """An object store that can be told to fail or to act in the middle of an operation."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_put = False
        self.fail_get = False
        self.fail_delete = False
        self.after_put: Callable[[], None] | None = None
        self.put_calls: list[tuple[str, bool]] = []

    def derived_keys(self) -> list[str]:
        return [k for k in self._objects if "/chunking/" in k]

    def put(
        self, key: TenantScopedKey, body: bytes, *, content_type: str, overwrite: bool = False
    ) -> StoredObject:
        if self.fail_put and "chunking/" in key.render():
            raise InfrastructureError("object_store_unavailable")
        self.put_calls.append((key.render(), overwrite))
        stored = super().put(key, body, content_type=content_type, overwrite=overwrite)
        if self.after_put is not None:
            hook, self.after_put = self.after_put, None  # once
            hook()
        return stored

    def get(self, key: TenantScopedKey) -> bytes:
        if self.fail_get:
            raise InfrastructureError("object_store_unavailable")
        return super().get(key)

    def delete(self, key: TenantScopedKey) -> None:
        if self.fail_delete:
            raise InfrastructureError("object_store_unavailable")
        super().delete(key)


@dataclass
class Rig:
    clock: FakeClock
    objects: FlakyObjectStore
    documents: InMemoryDocumentsUnitOfWorkFactory
    processing: InMemoryProcessingUnitOfWorkFactory
    chunking: InMemoryChunkingUnitOfWorkFactory
    upload: UploadDocument
    delete: DeleteDocument
    record_outcome: RecordVersionOutcome
    extract: ExtractStage
    reader: ReadExtraction
    stage: ChunkStage
    purge: PurgeChunks

    @property
    def store(self) -> InMemoryChunkingStore:
        return self.chunking.store

    def upload_document(
        self, body: str, tenant: TenantId = TENANT_A, *, extract: bool = True
    ) -> tuple[DocumentId, VersionId]:
        view = self.upload.execute(
            ctx(tenant),
            UploadDocumentCommand(title="Agreement", media_type=TEXT, body=body.encode("utf-8")),
        )
        document, version = DocumentId(view.document_id.value), VersionId(view.version_id.value)
        if extract:
            self.extract.run(self.request(document, version, tenant, stage="extract"))
        return document, version

    def request(
        self,
        document: DocumentId,
        version: VersionId,
        tenant: TenantId = TENANT_A,
        token: int = 1,
        stage: str = "chunk",
    ) -> StageRequest:
        return StageRequest(
            tenant,
            DocumentRef(document.value),
            VersionRef(version.value),
            IngestionRunId(UUID(int=0x4001)),
            stage,
            1,
            JobId(UUID(int=0x7001)),
            token,
        )

    def key(
        self, document: DocumentId, version: VersionId, tenant: TenantId = TENANT_A
    ) -> TenantScopedKey:
        return TenantScopedKey(tenant, chunks_path(document, version))

    def make_stage(self, parameters: ChunkingParameters) -> ChunkStage:
        return ChunkStage(self.reader, parameters, self.chunking, self.objects, self.clock)

    def mark(self, document: DocumentId, version: VersionId, status: VersionStatus) -> None:
        self.record_outcome.execute(
            SYSTEM_A,
            RecordOutcomeCommand(
                SourceDocumentId(document.value), SourceVersionId(version.value), status
            ),
        )

    def tombstone(self, document: DocumentId) -> None:
        self.delete.execute(ctx(), DocumentReference(SourceDocumentId(document.value)))


def build_rig(parameters: ChunkingParameters = PARAMS) -> Rig:
    clock = FakeClock(START)
    ids = SequentialIdGenerator()
    objects = FlakyObjectStore()
    documents = InMemoryDocumentsUnitOfWorkFactory(InMemoryDocumentsStore())
    processing = InMemoryProcessingUnitOfWorkFactory()
    chunking = InMemoryChunkingUnitOfWorkFactory()
    staging = UploadStaging(documents, objects, clock, ids, POLICY)
    reader = ReadExtraction(processing, objects, ReadVersionState(documents))
    return Rig(
        clock=clock,
        objects=objects,
        documents=documents,
        processing=processing,
        chunking=chunking,
        upload=UploadDocument(documents, staging, clock, ids),
        delete=DeleteDocument(documents, clock, ids),
        record_outcome=RecordVersionOutcome(documents, clock),
        extract=ExtractStage(
            ReadOriginal(documents, objects),
            MarkdownishParser(),
            processing,
            objects,
            clock,
            LIMITS,
        ),
        reader=reader,
        stage=ChunkStage(reader, parameters, chunking, objects, clock),
        purge=PurgeChunks(chunking, objects, clock),
    )
