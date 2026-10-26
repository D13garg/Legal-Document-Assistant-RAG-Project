"""Commands of citations (ADR-016). Claims are untrusted input; verified sources are the output."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from lda.modules.citations.domain.evidence import Citation, EvidenceSet
from lda.modules.citations.domain.ids import CitedDocumentId, CitedVersionId
from lda.modules.citations.domain.record import GenerationOutcome
from lda.shared.kernel.ids import RequestId


@dataclass(frozen=True, slots=True)
class SourceClaim:
    """A chunk reference as retrieval reported it. Nothing here is trusted: every field is checked
    against the chunk store, and the pages come from the store, never from the claim."""

    document_id: CitedDocumentId
    version_id: CitedVersionId
    chunk_id: UUID
    ordinal: int
    text_hash: str


@dataclass(frozen=True, slots=True)
class BuildEvidenceCommand:
    claims: tuple[SourceClaim, ...]
    proof_token: str = field(repr=False)
    """Minted for exactly `documents.read_document` over exactly the cited documents."""


@dataclass(frozen=True, slots=True)
class RecordGenerationCommand:
    request_id: RequestId
    evidence: EvidenceSet
    citations: tuple[Citation, ...]
    outcome: GenerationOutcome
    config_id: str
    template_id: str
    template_version: str
    model_id: str
    request_sha256: str = field(repr=False)
    """Content-derived and guessable for short prompts: never log it or return it to a client."""


@dataclass(frozen=True, slots=True)
class RecordedGeneration:
    record_id: UUID
