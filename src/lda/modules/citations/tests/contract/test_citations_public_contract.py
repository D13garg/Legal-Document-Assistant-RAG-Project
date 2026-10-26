"""The supported public surface of citations (ADR-016). `api.lock` pins signatures."""

from __future__ import annotations

from lda.modules.citations import api

EXPECTED = {
    "BuildEvidenceCommand",
    "CITATION_MISSING",
    "CITATION_NOT_IN_EVIDENCE",
    "ChunkProvenance",
    "ChunkProvenanceReader",
    "Citation",
    "CitationLimits",
    "CitationSource",
    "CitedDocumentId",
    "CitedVersionId",
    "EVIDENCE_FOREIGN",
    "EvidenceBuilder",
    "EvidenceSet",
    "GenerationOutcome",
    "GenerationRecord",
    "GenerationRecordRepository",
    "GenerationRecorder",
    "INVALID_INPUT",
    "LIMIT_EXCEEDED",
    "PROVENANCE_MISMATCH",
    "RECORD_EXISTS",
    "RECORD_INVALID",
    "ReadChunksProvenance",
    "RecordGenerationCommand",
    "RecordedGeneration",
    "SOURCE_UNAVAILABLE",
    "SourceClaim",
    "UNKNOWN_MARKER",
    "proof_resource",
    "source_resource",
    # Step 14 (ADR-016 amendment): additive text-bearing evidence
    "ChunkContent",
    "ChunkContentReader",
    "EvidencePassage",
    "ReadChunksContent",
    "TextEvidence",
    "TextEvidenceBuilder",
    "text_sha256",
}


def test_the_exported_names_are_exactly_the_reviewed_set() -> None:
    assert set(api.__all__) == EXPECTED
    assert all(hasattr(api, name) for name in api.__all__)


def test_codes_are_distinct_and_namespaced() -> None:
    codes = [getattr(api, n) for n in api.__all__ if n.isupper()]
    assert len(set(codes)) == len(codes)
    assert all(c.startswith("citations_") for c in codes)
