"""`EvidenceBuilder` and `TextEvidenceBuilder`: turn untrusted source claims into verified evidence
or refuse (ADR-016, ADR-016 amendment).

Neither is a use case: they keep no text, write nothing and grant nothing. Order, each step before
the next is attempted:
1. shape and limits of the claims;
2. the proof, for exactly `documents.read_document` over exactly the cited documents, BEFORE any
   chunk store is touched;
3. every claim is matched to the chunk store: same chunk id, same ordinal, same text hash. Pages
   come from the store, never from the claim;
4. the proof again, so a revocation during the read yields nothing;
5. (`TextEvidenceBuilder` only) every passage must hash to the recorded `text_hash`.
Any failure refuses the whole set (no partial evidence). A purged, deleted, foreign-tenant or
ineligible version is one code, `citations_source_unavailable`, so a caller cannot probe which.
Both builders share one verification routine, so text-bearing evidence can never be issued under
weaker checks than text-free evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from lda.modules.access_control.api import ActionId
from lda.modules.citations.application.dto import BuildEvidenceCommand, SourceClaim
from lda.modules.citations.application.limits import CitationLimits
from lda.modules.citations.application.ports import (
    ChunkContent,
    ChunkContentReader,
    ChunkProvenance,
    ChunkProvenanceReader,
)
from lda.modules.citations.application.resources import proof_resource
from lda.modules.citations.domain.codes import (
    INVALID_INPUT,
    LIMIT_EXCEEDED,
    PROVENANCE_MISMATCH,
    SOURCE_UNAVAILABLE,
)
from lda.modules.citations.domain.evidence import Citation, EvidenceSet, marker_for
from lda.modules.citations.domain.ids import CitedDocumentId, CitedVersionId
from lda.modules.citations.domain.source import READ_DOCUMENT, CitationSource
from lda.modules.citations.domain.text_evidence import EvidencePassage, TextEvidence
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.errors import ConflictError, NotFoundError, ValidationError
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.authorization import ProofExpectation, ProofVerifier


class EvidenceBuilder:
    def __init__(
        self,
        provenance: ChunkProvenanceReader,
        verifier: ProofVerifier,
        limits: CitationLimits,
    ) -> None:
        self._provenance = provenance
        self._verifier = verifier
        self._limits = limits

    def build(self, ctx: TenantContext, command: BuildEvidenceCommand) -> EvidenceSet:
        evidence, _ = _verify(ctx, command, self._limits, self._verifier, self._provenance.read)
        return evidence


class TextEvidenceBuilder:
    """Verified evidence plus the stored text of every cited chunk (ADR-016 amendment).

    The text is read only after the proof has been verified, and only through the injected
    `ChunkContentReader` (chunking's `ReadChunks`). Text a caller supplied can never enter: there
    is no parameter for it.
    """

    def __init__(
        self,
        content: ChunkContentReader,
        verifier: ProofVerifier,
        limits: CitationLimits,
    ) -> None:
        self._content = content
        self._verifier = verifier
        self._limits = limits

    def build(self, ctx: TenantContext, command: BuildEvidenceCommand) -> TextEvidence:
        evidence, stored = _verify(ctx, command, self._limits, self._verifier, self._content.read)
        passages: list[EvidencePassage] = []
        for citation, chunk in zip(evidence.citations, stored, strict=True):
            if not isinstance(chunk, ChunkContent):
                raise ValidationError(PROVENANCE_MISMATCH)
            passages.append(EvidencePassage(citation.marker, chunk.text))
        return TextEvidence(evidence, tuple(passages))  # re-checks every passage hash


def _verify(
    ctx: TenantContext,
    command: BuildEvidenceCommand,
    limits: CitationLimits,
    verifier: ProofVerifier,
    read: Callable[[TenantId, CitedDocumentId, CitedVersionId], Sequence[ChunkProvenance]],
) -> tuple[EvidenceSet, tuple[ChunkProvenance, ...]]:
    claims = _checked(command, limits)
    expected = ProofExpectation(
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        action=str(ActionId(READ_DOCUMENT)),
        resources=frozenset(proof_resource(c.document_id) for c in claims),
    )
    verifier.verify(command.proof_token, expected)
    chunks: dict[tuple[CitedDocumentId, CitedVersionId], dict[object, ChunkProvenance]] = {}
    citations: list[Citation] = []
    matched: list[ChunkProvenance] = []
    for position, claim in enumerate(claims, start=1):
        key = (claim.document_id, claim.version_id)
        if key not in chunks:
            chunks[key] = _load(read, ctx, *key)
        stored = chunks[key].get(claim.chunk_id)
        if stored is None or (stored.ordinal, stored.text_hash) != (claim.ordinal, claim.text_hash):
            raise ValidationError(PROVENANCE_MISMATCH)
        source = CitationSource(
            claim.document_id,
            claim.version_id,
            stored.chunk_id,
            stored.ordinal,
            stored.text_hash,
            stored.page_start,
            stored.page_end,
        )
        citations.append(Citation(marker_for(position), source))
        matched.append(stored)
    verifier.verify(command.proof_token, expected)
    return EvidenceSet(ctx.tenant_id, ctx.principal_id, tuple(citations)), tuple(matched)


def _checked(command: BuildEvidenceCommand, limits: CitationLimits) -> tuple[SourceClaim, ...]:
    claims = command.claims
    if not isinstance(claims, tuple) or not claims:
        raise ValidationError(INVALID_INPUT, detail="claims")
    if len(claims) > limits.max_sources:
        raise ValidationError(LIMIT_EXCEEDED, detail="max_sources")
    if not isinstance(command.proof_token, str) or not command.proof_token:
        raise ValidationError(INVALID_INPUT, detail="proof_token")
    for claim in claims:
        if not isinstance(claim, SourceClaim):
            raise ValidationError(INVALID_INPUT, detail="claims")
        CitationSource(  # shape check only: ids, ordinal, hash
            claim.document_id, claim.version_id, claim.chunk_id, claim.ordinal, claim.text_hash
        )
    if len({c.chunk_id for c in claims}) != len(claims):
        raise ValidationError(INVALID_INPUT, detail="claims")
    return claims


def _load(
    read: Callable[[TenantId, CitedDocumentId, CitedVersionId], Sequence[ChunkProvenance]],
    ctx: TenantContext,
    document_id: CitedDocumentId,
    version_id: CitedVersionId,
) -> dict[object, ChunkProvenance]:
    try:
        found = read(ctx.tenant_id, document_id, version_id)
    except (NotFoundError, ConflictError):
        raise NotFoundError(SOURCE_UNAVAILABLE) from None  # one code; the cause is discarded
    return {c.chunk_id: c for c in found}
