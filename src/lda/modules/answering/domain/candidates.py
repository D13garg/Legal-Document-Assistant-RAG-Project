"""The caller-supplied candidate documents (ADR-040 Q2): untrusted, bounded, duplicate-free."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from lda.modules.answering.domain import codes
from lda.shared.kernel.errors import ValidationError


@dataclass(frozen=True, slots=True)
class CandidateDocuments:
    """A non-empty tuple of distinct document ids no longer than the configured maximum.

    Membership says nothing about access: Access Control decides which of these the caller may
    read, and only that subset is ever searched. Nothing here widens the set.
    """

    ids: tuple[UUID, ...]

    @classmethod
    def within(cls, raw: object, max_candidates: int) -> CandidateDocuments:
        if (
            not isinstance(raw, tuple)
            or not raw
            or len(raw) > max_candidates
            or not all(isinstance(i, UUID) for i in raw)
            or len(set(raw)) != len(raw)
        ):
            raise ValidationError(codes.INVALID_CANDIDATES)
        return cls(raw)
