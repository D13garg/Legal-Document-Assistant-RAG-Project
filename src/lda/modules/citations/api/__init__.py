"""Public surface of citations (ADR-016): verified source references, marker resolution and the
immutable `GenerationRecord` contract.

A citation is a reference to a chunk of an immutable version, derived from chunk provenance, never
from answer text. It is not an authorization: showing or reading a cited source needs a fresh
decision. No text, excerpt, section title or offset is carried or stored. Records are written
through the insert-only `GenerationRecordRepository` port; production uses the PostgreSQL adapter in
`citations.infrastructure` (schema `citations`, forced RLS, insert-only; ADR-016 amendment) and
tests may use the in-memory twin. The adapter is not part of this public surface.

Text-bearing evidence (ADR-016 amendment, Step 14): `TextEvidenceBuilder` issues a `TextEvidence`
only after the same proof and provenance checks as `EvidenceBuilder`, reading chunk text solely
through `ReadChunksContent` (chunking's `ReadChunks`) and verifying each passage against its
recorded hash. Text is never persisted, logged or accepted from a caller.
"""

from lda.modules.citations.application.content import ReadChunksContent
from lda.modules.citations.application.dto import (
    BuildEvidenceCommand,
    RecordedGeneration,
    RecordGenerationCommand,
    SourceClaim,
)
from lda.modules.citations.application.evidence_builder import (
    EvidenceBuilder,
    TextEvidenceBuilder,
)
from lda.modules.citations.application.generation_recorder import GenerationRecorder
from lda.modules.citations.application.limits import CitationLimits
from lda.modules.citations.application.ports import (
    ChunkContent,
    ChunkContentReader,
    ChunkProvenance,
    ChunkProvenanceReader,
    GenerationRecordRepository,
)
from lda.modules.citations.application.provenance import ReadChunksProvenance
from lda.modules.citations.application.resources import proof_resource, source_resource
from lda.modules.citations.domain.codes import (
    CITATION_MISSING,
    CITATION_NOT_IN_EVIDENCE,
    EVIDENCE_FOREIGN,
    INVALID_INPUT,
    LIMIT_EXCEEDED,
    PROVENANCE_MISMATCH,
    RECORD_EXISTS,
    RECORD_INVALID,
    SOURCE_UNAVAILABLE,
    UNKNOWN_MARKER,
)
from lda.modules.citations.domain.evidence import Citation, EvidenceSet
from lda.modules.citations.domain.ids import CitedDocumentId, CitedVersionId
from lda.modules.citations.domain.record import GenerationOutcome, GenerationRecord
from lda.modules.citations.domain.source import CitationSource
from lda.modules.citations.domain.text_evidence import EvidencePassage, TextEvidence, text_sha256

__all__ = [
    "CITATION_MISSING",
    "CITATION_NOT_IN_EVIDENCE",
    "EVIDENCE_FOREIGN",
    "INVALID_INPUT",
    "LIMIT_EXCEEDED",
    "PROVENANCE_MISMATCH",
    "RECORD_EXISTS",
    "RECORD_INVALID",
    "SOURCE_UNAVAILABLE",
    "UNKNOWN_MARKER",
    "BuildEvidenceCommand",
    "ChunkContent",
    "ChunkContentReader",
    "ChunkProvenance",
    "ChunkProvenanceReader",
    "Citation",
    "CitationLimits",
    "CitationSource",
    "CitedDocumentId",
    "CitedVersionId",
    "EvidenceBuilder",
    "EvidencePassage",
    "EvidenceSet",
    "GenerationOutcome",
    "GenerationRecord",
    "GenerationRecordRepository",
    "GenerationRecorder",
    "ReadChunksContent",
    "ReadChunksProvenance",
    "RecordGenerationCommand",
    "RecordedGeneration",
    "SourceClaim",
    "TextEvidence",
    "TextEvidenceBuilder",
    "proof_resource",
    "source_resource",
    "text_sha256",
]
