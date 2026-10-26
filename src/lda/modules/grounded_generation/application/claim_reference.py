"""A neutral, validated reference to one retrieved chunk (ADR-040 Q1).

Callers that may not depend on `citations` (the DAG gives `answering` no such edge) describe a
retrieval hit with plain identifiers; this module turns it into the `citations.api.SourceClaim`
that `GenerateGroundedAnswerCommand` takes. A reference is untrusted, exactly like a claim: it is
shape-checked here and every field is checked against the chunk store later, and it is never proof
of authorization.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from lda.modules.citations.api import CitedDocumentId, CitedVersionId, SourceClaim
from lda.modules.grounded_generation.domain import codes
from lda.shared.kernel.errors import ValidationError

MAX_ORDINAL = 1_000_000
"""Same ceiling as the citations source contract; a larger ordinal is refused here."""
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ClaimReference:
    """Plain identifiers of one retrieved chunk: document, version, chunk, ordinal, content hash."""

    document_id: UUID
    version_id: UUID
    chunk_id: UUID
    ordinal: int
    text_hash: str

    def __post_init__(self) -> None:
        if not all(isinstance(i, UUID) for i in (self.document_id, self.version_id, self.chunk_id)):
            raise ValidationError(codes.INVALID_INPUT, detail="claim_reference_ids")
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or not 0 <= self.ordinal <= MAX_ORDINAL
        ):
            raise ValidationError(codes.INVALID_INPUT, detail="claim_reference_ordinal")
        if not isinstance(self.text_hash, str) or _DIGEST.fullmatch(self.text_hash) is None:
            raise ValidationError(codes.INVALID_INPUT, detail="claim_reference_hash")


def claims_from_references(references: tuple[ClaimReference, ...]) -> tuple[SourceClaim, ...]:
    """Convert references, in order, to the claims of `GenerateGroundedAnswerCommand`.

    The order is kept because rank order becomes marker order. An empty tuple stays empty (the use
    case answers `INSUFFICIENT_EVIDENCE` for it). A non-tuple or a foreign element is refused.
    """
    if not isinstance(references, tuple) or not all(
        isinstance(r, ClaimReference) for r in references
    ):
        raise ValidationError(codes.INVALID_INPUT, detail="claim_references")
    return tuple(
        SourceClaim(
            CitedDocumentId(r.document_id),
            CitedVersionId(r.version_id),
            r.chunk_id,
            r.ordinal,
            r.text_hash,
        )
        for r in references
    )
