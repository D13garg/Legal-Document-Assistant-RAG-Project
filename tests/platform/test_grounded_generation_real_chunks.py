"""Text evidence over the REAL chunking pipeline and the REAL `ReadChunks` (ADR-016 amendment 2).

The unit tests of `TextEvidenceBuilder` use a fake chunk store. This test closes the gap that fake
leaves open: that `text_sha256` is the hash chunking really records, that `ReadChunksContent`
returns the real stored text, and that authorization still precedes the text read.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from lda.modules.chunking.api import ReadChunks, ReadChunksCommand
from lda.modules.chunking.tests.support import SECRET, SYSTEM_A, TENANT_A, Rig, build_rig, ctx
from lda.modules.citations.api import (
    BuildEvidenceCommand,
    CitationLimits,
    CitedDocumentId,
    CitedVersionId,
    ReadChunksContent,
    SourceClaim,
    TextEvidenceBuilder,
    text_sha256,
)
from lda.modules.citations.testing import RecordingVerifier
from lda.modules.documents.api import ReadVersionState, VersionStatus
from lda.shared.kernel.errors import AuthorizationError, NotFoundError
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.authorization import ProofExpectation, ProofResource

BODY = (
    f"# Terms\n\n{SECRET} applies to every party.\n\n## Fees\n\n"
    "Fees are due monthly in advance, without set-off.\n\n"
    "## Notice\n\nThe Supplier shall give ninety days written notice."
)
TOKEN = "proof-token"  # noqa: S105 - a test credential


@pytest.fixture
def rig() -> Rig:
    return build_rig()


def chunked(rig: Rig) -> tuple[ReadChunks, CitedDocumentId, CitedVersionId]:
    document, version = rig.upload_document(BODY)
    assert rig.stage.run(rig.request(document, version)).__class__.__name__ == "StageSucceeded"
    rig.mark(document, version, VersionStatus.READY)
    reader = ReadChunks(rig.chunking, rig.objects, ReadVersionState(rig.documents))
    return reader, CitedDocumentId(document.value), CitedVersionId(version.value)


def claims_for(reader: ReadChunks, rig: Rig, document: CitedDocumentId, version: CitedVersionId):  # type: ignore[no-untyped-def]
    from lda.modules.chunking.api import DocumentId, VersionId

    stored = reader.execute(
        SYSTEM_A, ReadChunksCommand(DocumentId(document.value), VersionId(version.value))
    )
    return stored, tuple(
        SourceClaim(document, version, c.chunk_id, c.ordinal, c.text_hash) for c in stored.chunks
    )


def verifier_for(document: CitedDocumentId) -> RecordingVerifier:
    return RecordingVerifier(
        TOKEN,
        ProofExpectation(
            tenant_id=TENANT_A,
            principal_id=ctx().principal_id,
            action="documents.read_document",
            resources=frozenset({ProofResource("document", str(document.value))}),
        ),
    )


def test_the_real_chunk_text_and_hash_round_trip_through_the_evidence_builder(rig: Rig) -> None:
    reader, document, version = chunked(rig)
    stored, claims = claims_for(reader, rig, document, version)
    builder = TextEvidenceBuilder(
        ReadChunksContent(reader), verifier_for(document), CitationLimits(50, 50)
    )
    built = builder.build(ctx(), BuildEvidenceCommand(claims, TOKEN))
    assert [p.text for p in built.passages] == [c.text for c in stored.chunks]
    assert all(
        text_sha256(p.text) == c.text_hash
        for p, c in zip(built.passages, stored.chunks, strict=True)
    )
    assert SECRET in "".join(p.text for p in built.passages)
    assert SECRET not in repr(built) and SECRET not in repr(built.passages)


def test_a_forged_claim_hash_is_refused_by_the_real_store(rig: Rig) -> None:
    reader, document, version = chunked(rig)
    _, claims = claims_for(reader, rig, document, version)
    forged = (SourceClaim(document, version, claims[0].chunk_id, claims[0].ordinal, "0" * 64),)
    builder = TextEvidenceBuilder(
        ReadChunksContent(reader), verifier_for(document), CitationLimits(50, 50)
    )
    with pytest.raises(Exception) as caught:
        builder.build(ctx(), BuildEvidenceCommand(forged, TOKEN))
    assert getattr(caught.value, "code", "") == "citations_provenance_mismatch"


def test_no_text_is_read_without_a_valid_proof(rig: Rig) -> None:
    reader, document, version = chunked(rig)
    _, claims = claims_for(reader, rig, document, version)
    reads = []

    class Counting(ReadChunksContent):
        def read(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            reads.append(1)
            return super().read(*args, **kwargs)

    builder = TextEvidenceBuilder(
        Counting(reader),
        RecordingVerifier("other", verifier_for(document).accepted),
        CitationLimits(50, 50),
    )
    with pytest.raises(AuthorizationError):
        builder.build(ctx(), BuildEvidenceCommand(claims, TOKEN))
    assert reads == []


def test_a_tombstoned_document_serves_no_text(rig: Rig) -> None:
    reader, document, version = chunked(rig)
    _, claims = claims_for(reader, rig, document, version)
    from lda.modules.chunking.api import DocumentId

    rig.tombstone(DocumentId(document.value))
    builder = TextEvidenceBuilder(
        ReadChunksContent(reader), verifier_for(document), CitationLimits(50, 50)
    )
    with pytest.raises(NotFoundError) as caught:
        builder.build(ctx(), BuildEvidenceCommand(claims, TOKEN))
    assert caught.value.code == "citations_source_unavailable"


def test_another_tenants_chunks_are_unavailable(rig: Rig) -> None:
    reader, document, version = chunked(rig)
    _, claims = claims_for(reader, rig, document, version)
    stranger = ctx(TenantId(UUID(int=2)))
    builder = TextEvidenceBuilder(
        ReadChunksContent(reader),
        RecordingVerifier(
            TOKEN,
            ProofExpectation(
                tenant_id=stranger.tenant_id,
                principal_id=stranger.principal_id,
                action="documents.read_document",
                resources=frozenset({ProofResource("document", str(document.value))}),
            ),
        ),
        CitationLimits(50, 50),
    )
    with pytest.raises(NotFoundError):
        builder.build(stranger, BuildEvidenceCommand(claims, TOKEN))
