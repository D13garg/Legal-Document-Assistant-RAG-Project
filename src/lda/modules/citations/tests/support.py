"""Shared builders for the citations tests: one tenant, one principal, two chunked versions."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from lda.modules.citations.api import (
    BuildEvidenceCommand,
    ChunkProvenance,
    CitationLimits,
    EvidenceBuilder,
    EvidenceSet,
    SourceClaim,
)
from lda.modules.citations.api import (
    CitedDocumentId as DocumentId,
)
from lda.modules.citations.api import (
    CitedVersionId as VersionId,
)
from lda.modules.citations.domain.source import DOCUMENT_KIND, READ_DOCUMENT
from lda.modules.citations.testing import FakeProvenance, RecordingVerifier
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId
from lda.shared.ports.authorization import ProofExpectation, ProofResource

TOKEN = "proof-token"  # noqa: S105 - a test double, not a credential


def digest(n: int) -> str:
    return f"{n:064x}"


def new_ctx(tenant: TenantId | None = None) -> TenantContext:
    return TenantContext(tenant or TenantId(uuid4()), PrincipalId(uuid4()), RequestId(uuid4()))


@dataclass
class Scenario:
    ctx: TenantContext = field(default_factory=new_ctx)
    doc_a: DocumentId = field(default_factory=lambda: DocumentId(uuid4()))
    ver_a: VersionId = field(default_factory=lambda: VersionId(uuid4()))
    doc_b: DocumentId = field(default_factory=lambda: DocumentId(uuid4()))
    ver_b: VersionId = field(default_factory=lambda: VersionId(uuid4()))
    provenance: FakeProvenance = field(default_factory=FakeProvenance)
    chunks_a: tuple[ChunkProvenance, ...] = ()
    chunks_b: tuple[ChunkProvenance, ...] = ()

    def __post_init__(self) -> None:
        self.chunks_a = (
            ChunkProvenance(uuid4(), 0, digest(1), 1, 1),
            ChunkProvenance(uuid4(), 1, digest(2), 1, 2),
            ChunkProvenance(uuid4(), 2, digest(3), None, None),
        )
        self.chunks_b = (ChunkProvenance(uuid4(), 0, digest(4), 7, 9),)
        self.provenance.put(self.ctx.tenant_id, self.doc_a, self.ver_a, self.chunks_a)
        self.provenance.put(self.ctx.tenant_id, self.doc_b, self.ver_b, self.chunks_b)

    def claim(self, which: str = "a", index: int = 0) -> SourceClaim:
        doc, ver, chunks = (
            (self.doc_a, self.ver_a, self.chunks_a)
            if which == "a"
            else (self.doc_b, self.ver_b, self.chunks_b)
        )
        c = chunks[index]
        return SourceClaim(doc, ver, c.chunk_id, c.ordinal, c.text_hash)

    def expectation(self, *docs: DocumentId) -> ProofExpectation:
        return ProofExpectation(
            tenant_id=self.ctx.tenant_id,
            principal_id=self.ctx.principal_id,
            action=READ_DOCUMENT,
            resources=frozenset(ProofResource(DOCUMENT_KIND, str(d.value)) for d in docs),
        )

    def verifier(self, *docs: DocumentId) -> RecordingVerifier:
        return RecordingVerifier(TOKEN, self.expectation(*docs))

    def builder(
        self, verifier: RecordingVerifier | None = None, max_sources: int = 10
    ) -> EvidenceBuilder:
        return EvidenceBuilder(
            self.provenance,
            verifier or self.verifier(self.doc_a, self.doc_b),
            CitationLimits(max_sources, 20),
        )

    def evidence(self, *claims: SourceClaim) -> EvidenceSet:
        use = claims or (self.claim("a", 0), self.claim("a", 1), self.claim("b", 0))
        docs = tuple({c.document_id: None for c in use})
        return self.builder(self.verifier(*docs)).build(
            self.ctx, BuildEvidenceCommand(tuple(use), TOKEN)
        )


def some_uuid() -> UUID:
    return uuid4()
