"""What crosses the answering boundary (ADR-040): identifiers, the caller's own question and, for
one response only, the validated answer statements. Statement text is never stored by this module
and never appears in a repr."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from lda.modules.answering.domain import codes
from lda.modules.answering.domain.vocabulary import AnswerOutcome
from lda.modules.conversations.api import AppendedMessageView, ClientMessageId, ConversationId
from lda.modules.grounded_generation.api import AnswerStatement
from lda.shared.kernel.errors import ValidationError


@dataclass(frozen=True, slots=True)
class AnswerCommand:
    conversation_id: ConversationId
    client_message_id: ClientMessageId
    question: str = field(repr=False)
    candidate_document_ids: tuple[UUID, ...] = field(repr=False)
    """Untrusted. Access Control decides which of these the caller may read; nothing else is
    searched, and there is no 'search everything I may read' mode (ADR-040 Q2)."""


@dataclass(frozen=True, slots=True)
class AnsweredTurn:
    """The completed turn. Returned only after every required conversation write succeeded.

    `statements` is the one and only copy of the answer text: it is not stored anywhere and cannot
    be recovered later (ADR-039 C1). `assistant_message` and `record_id` are both None exactly
    when nothing was generated (no usable evidence), so no turn could be linked.
    """

    outcome: AnswerOutcome
    statements: tuple[AnswerStatement, ...] = field(repr=False)
    user_message: AppendedMessageView
    record_id: UUID | None = None
    assistant_message: AppendedMessageView | None = None
    rejection: str | None = None

    def __post_init__(self) -> None:
        answered = self.outcome is AnswerOutcome.ANSWERED
        rejected = self.outcome is AnswerOutcome.REJECTED
        linked = self.assistant_message is not None
        if (
            not isinstance(self.outcome, AnswerOutcome)
            or answered != bool(self.statements)
            or rejected != (self.rejection is not None)
            or linked != (self.record_id is not None)
            or (
                self.assistant_message is not None
                and self.assistant_message.message.generation_record_id != self.record_id
            )
            or (self.record_id is None and self.outcome is not AnswerOutcome.INSUFFICIENT_EVIDENCE)
        ):
            raise ValidationError(codes.INVALID_COMMAND, detail="answered_turn")
