"""`EvidenceAssembler`: authorization first, then verified text evidence, or refuse (ADR-029).

Order, each step before the next is attempted:
1. the claims are bounded (so the authorization request is bounded);
2. a FRESH decision for exactly `documents.read_document` over exactly the cited documents. The
   claims came from a search the caller ran earlier; that search proves nothing now;
3. only with that proof, `TextEvidenceBuilder` verifies it again, reads the chunk store and checks
   every claim and every passage hash.
No chunk text is read, built or held before step 2 has succeeded, and a denial reveals nothing about
which document, claim or tenant caused it.
"""

from __future__ import annotations

from lda.modules.citations.api import (
    BuildEvidenceCommand,
    CitedDocumentId,
    SourceClaim,
    TextEvidence,
    TextEvidenceBuilder,
)
from lda.modules.grounded_generation.application.ports import DocumentAccess
from lda.modules.grounded_generation.domain import codes
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.errors import ValidationError


class EvidenceAssembler:
    def __init__(
        self, access: DocumentAccess, builder: TextEvidenceBuilder, max_sources: int
    ) -> None:
        self._access = access
        self._builder = builder
        self._max_sources = max_sources

    def assemble(self, ctx: TenantContext, claims: tuple[SourceClaim, ...]) -> TextEvidence:
        if not isinstance(claims, tuple) or not claims:
            raise ValidationError(codes.INVALID_INPUT, detail="claims")
        if len(claims) > self._max_sources:
            raise ValidationError(codes.LIMIT_EXCEEDED, detail="max_sources")
        if not all(isinstance(c, SourceClaim) for c in claims):
            raise ValidationError(codes.INVALID_INPUT, detail="claims")
        documents = frozenset(_document_of(c) for c in claims)
        token = self._access.authorize_read(ctx, documents)  # 2. before anything is read
        return self._builder.build(ctx, BuildEvidenceCommand(claims, token.reveal()))  # 3.


def _document_of(claim: SourceClaim) -> CitedDocumentId:
    if not isinstance(claim.document_id, CitedDocumentId):
        raise ValidationError(codes.INVALID_INPUT, detail="claims")
    return claim.document_id
