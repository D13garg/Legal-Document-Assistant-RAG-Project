"""Enforced bounds (ADR-039 C5): they bound message size, conversation size and deletion work."""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.conversation import HARD_MAX_MESSAGES
from lda.modules.conversations.domain.text import HARD_MAX_TEXT_CHARS
from lda.shared.kernel.errors import ValidationError

HARD_MAX_LIST = 200


@dataclass(frozen=True, slots=True)
class ConversationLimits:
    max_messages: int
    max_text_chars: int
    max_list: int

    def __post_init__(self) -> None:
        if (
            not 1 <= self.max_messages <= HARD_MAX_MESSAGES
            or not 1 <= self.max_text_chars <= HARD_MAX_TEXT_CHARS
            or not 1 <= self.max_list <= HARD_MAX_LIST
        ):
            raise ValidationError(codes.LIMITS_INVALID)
