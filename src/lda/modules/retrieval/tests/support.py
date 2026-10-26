"""Rig: real Documents (in-memory, via its public API) + retrieval with fakes for the embedder, the
verifier and the chunk source. Status, number and tombstone come from the real Documents code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from lda.modules.access_control.api import ResourceRef
from lda.modules.access_control.testing import an_access_scope
from lda.modules.documents.api import (
    DOCUMENT_KIND,
    READ_DOCUMENT,
    AddDocumentVersion,
    AddDocumentVersionCommand,
    DeleteDocument,
    DocumentReference,
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
from lda.modules.retrieval.api import (
    IndexStage,
    PurgeCommand,
    PurgeRetrievalIndex,
    SearchIndex,
    SearchLimits,
    SearchQuery,
    SearchResult,
)
from lda.modules.retrieval.application.ports import RetrievalUnitOfWorkFactory, VersionStates
from lda.modules.retrieval.domain.ids import DocumentId, VersionId
from lda.modules.retrieval.infrastructure.gateways import DocumentsVersionStates
from lda.modules.retrieval.testing import (
    FakeChunkSource,
    FakeEmbedder,
    FakeProofVerifier,
    InMemoryRetrievalUnitOfWorkFactory,
)
from lda.platform.testing.doubles import FakeClock, InMemoryObjectStore, SequentialIdGenerator
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.ids import JobId, PrincipalId, RequestId, TenantId
from lda.shared.ports.authorization import ProofExpectation, ProofResource

TENANT_A = TenantId(UUID(int=0xA1))
TENANT_B = TenantId(UUID(int=0xB2))
ALICE = PrincipalId(UUID(int=0xA11CE))
BOB = PrincipalId(UUID(int=0xB0B))
START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
POLICY = UploadPolicy(
    max_bytes=100_000, allowed_media_types=frozenset({"text/plain"}), grace_seconds=900
)
LIMITS = SearchLimits(max_results=10, max_query_characters=500, candidate_pool=4, max_candidates=40)
SECRET = "Confidential indemnity clause"  # noqa: S105
SYSTEM_A = SystemContext(TENANT_A, "retrieval-test")


def ctx(tenant: TenantId = TENANT_A, principal: PrincipalId = ALICE, n: int = 1) -> TenantContext:
    return TenantContext(tenant, principal, RequestId(UUID(int=n)))


@dataclass
class Rig:
    clock: FakeClock
    documents: InMemoryDocumentsUnitOfWorkFactory
    units: RetrievalUnitOfWorkFactory
    states: VersionStates
    embedder: FakeEmbedder
    verifier: FakeProofVerifier
    chunks: FakeChunkSource
    upload: UploadDocument
    add_version: AddDocumentVersion
    delete: DeleteDocument
    record_outcome: RecordVersionOutcome
    stage: IndexStage
    search_index: SearchIndex
    purge: PurgeRetrievalIndex
    limits: SearchLimits

    @property
    def retrieval(self) -> InMemoryRetrievalUnitOfWorkFactory:
        """The in-memory factory (tests that look at stored rows); SQL rigs use `units`."""
        assert isinstance(self.units, InMemoryRetrievalUnitOfWorkFactory)
        return self.units

    def new_document(
        self, texts: list[str], tenant: TenantId = TENANT_A
    ) -> tuple[DocumentId, VersionId]:
        view = self.upload.execute(
            ctx(tenant), UploadDocumentCommand("Agreement", "text/plain", b"v1")
        )
        document, version = DocumentId(view.document_id.value), VersionId(view.version_id.value)
        self.chunks.put(document, version, texts)
        return document, version

    def new_version(self, document: DocumentId, texts: list[str]) -> VersionId:
        view = self.add_version.execute(
            ctx(), AddDocumentVersionCommand(SourceDocumentId(document.value), "text/plain", b"v2")
        )
        version = VersionId(view.version_id.value)
        self.chunks.put(document, version, texts)
        return version

    def request(
        self, document: DocumentId, version: VersionId, tenant: TenantId = TENANT_A, token: int = 1
    ) -> StageRequest:
        return StageRequest(
            tenant,
            DocumentRef(document.value),
            VersionRef(version.value),
            IngestionRunId(UUID(int=0x4001)),
            "index",
            1,
            JobId(UUID(int=0x7001)),
            token,
        )

    def index(self, document: DocumentId, version: VersionId, tenant: TenantId = TENANT_A):  # type: ignore[no-untyped-def]
        return self.stage.run(self.request(document, version, tenant))

    def mark(self, document: DocumentId, version: VersionId, status: VersionStatus) -> None:
        self.record_outcome.execute(
            SYSTEM_A,
            RecordOutcomeCommand(
                SourceDocumentId(document.value), SourceVersionId(version.value), status
            ),
        )

    def ready(self, document: DocumentId, version: VersionId) -> None:
        self.mark(document, version, VersionStatus.READY)

    def tombstone(self, document: DocumentId) -> None:
        self.delete.execute(ctx(), DocumentReference(SourceDocumentId(document.value)))

    def query(
        self,
        text: str,
        documents: list[DocumentId],
        *,
        limit: int = 5,
        tenant: TenantId = TENANT_A,
        principal: PrincipalId = ALICE,
        proof_id: str = "proof-1",
        grant: bool = True,
    ) -> SearchQuery:
        resources = frozenset(ResourceRef(DOCUMENT_KIND, str(d.value)) for d in documents)
        scope = an_access_scope(tenant_id=tenant, principal_id=principal, resources=resources)
        if grant:
            self.verifier.grant(
                proof_id,
                ProofExpectation(
                    tenant_id=tenant,
                    principal_id=principal,
                    action=READ_DOCUMENT,
                    resources=frozenset(ProofResource(r.kind, r.id) for r in resources),
                ),
            )
        return SearchQuery(scope, proof_id, text, limit)

    def search(self, text: str, documents: list[DocumentId], **kw) -> SearchResult:  # type: ignore[no-untyped-def]
        return self.search_index.execute(ctx(), self.query(text, documents, **kw))

    def purge_document(self, document: DocumentId):  # type: ignore[no-untyped-def]
        return self.purge.execute(SYSTEM_A, PurgeCommand(document))


def build_rig(
    limits: SearchLimits = LIMITS, retrieval: RetrievalUnitOfWorkFactory | None = None
) -> Rig:
    clock = FakeClock(START)
    ids = SequentialIdGenerator()
    objects = InMemoryObjectStore()
    documents = InMemoryDocumentsUnitOfWorkFactory(InMemoryDocumentsStore())
    retrieval = retrieval or InMemoryRetrievalUnitOfWorkFactory()
    embedder, verifier, chunks = FakeEmbedder(), FakeProofVerifier(), FakeChunkSource()
    staging = UploadStaging(documents, objects, clock, ids, POLICY)
    states = DocumentsVersionStates(ReadVersionState(documents))
    return Rig(
        clock=clock,
        documents=documents,
        units=retrieval,
        states=states,
        embedder=embedder,
        verifier=verifier,
        chunks=chunks,
        upload=UploadDocument(documents, staging, clock, ids),
        add_version=AddDocumentVersion(documents, staging, clock, ids),
        delete=DeleteDocument(documents, clock, ids),
        record_outcome=RecordVersionOutcome(documents, clock),
        stage=IndexStage(chunks, embedder, retrieval, clock, batch_size=2),
        search_index=SearchIndex(retrieval, embedder, verifier, states, limits),
        purge=PurgeRetrievalIndex(retrieval, clock),
        limits=limits,
    )
