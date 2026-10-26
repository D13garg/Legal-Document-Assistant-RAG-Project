"""Public surface of answering (ADR-040): one use case, its command, result, limits and codes.

`Answer` appends the owner's question to a conversation, finds evidence among the caller-named
documents the caller may read, generates a validated, cited answer through `grounded_generation`
and links the assistant turn to its generation record. The answer text is returned once and is not
stored. Collaborators are the public use cases of the modules it calls, composed by bootstrap.
"""

from lda.modules.answering.application.dto import AnswerCommand, AnsweredTurn
from lda.modules.answering.application.limits import AnswerLimits
from lda.modules.answering.application.ports import (
    AnswerGenerator,
    AssistantTurnWriter,
    ChunkSearch,
    ScopeResolver,
    UserMessageWriter,
)
from lda.modules.answering.application.use_cases.answer import Answer
from lda.modules.answering.domain import codes
from lda.modules.answering.domain.vocabulary import AnswerOutcome

INVALID_CANDIDATES = codes.INVALID_CANDIDATES
INVALID_COMMAND = codes.INVALID_COMMAND
INVALID_QUESTION = codes.INVALID_QUESTION
LIMITS_INVALID = codes.LIMITS_INVALID
NOT_AUTHORIZED = codes.NOT_AUTHORIZED
REQUIRES_PRINCIPAL = codes.REQUIRES_PRINCIPAL
TURN_NOT_RECORDED = codes.TURN_NOT_RECORDED

__all__ = [
    "INVALID_CANDIDATES",
    "INVALID_COMMAND",
    "INVALID_QUESTION",
    "LIMITS_INVALID",
    "NOT_AUTHORIZED",
    "REQUIRES_PRINCIPAL",
    "TURN_NOT_RECORDED",
    "Answer",
    "AnswerCommand",
    "AnswerGenerator",
    "AnswerLimits",
    "AnswerOutcome",
    "AnsweredTurn",
    "AssistantTurnWriter",
    "ChunkSearch",
    "ScopeResolver",
    "UserMessageWriter",
]
