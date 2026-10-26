"""Enforced bounds of one answering request (ADR-040)."""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.answering.domain import codes
from lda.modules.answering.domain.vocabulary import CONFIG_ID
from lda.shared.kernel.errors import ValidationError

HARD_MAX_CANDIDATES = 10_000  # equals access_control's MAX_SCOPE_RESOURCES; a test pins it
HARD_MAX_QUESTION_CHARACTERS = 20_000
HARD_MAX_SOURCES = 100


@dataclass(frozen=True, slots=True)
class AnswerLimits:
    config_id: str
    max_candidates: int
    max_question_characters: int
    max_sources: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.config_id, str)
            or CONFIG_ID.fullmatch(self.config_id) is None
            or not 1 <= self.max_candidates <= HARD_MAX_CANDIDATES
            or not 1 <= self.max_question_characters <= HARD_MAX_QUESTION_CHARACTERS
            or not 1 <= self.max_sources <= HARD_MAX_SOURCES
        ):
            raise ValidationError(codes.LIMITS_INVALID)
