"""Messages are immutable once created. A user message holds the user's text; an assistant message
holds only the identifier of the `GenerationRecord` it refers to (ADR-039 C1, C2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.conversation import HARD_MAX_MESSAGES
from lda.modules.conversations.domain.ids import ClientMessageId, ConversationId
from lda.modules.conversations.domain.text import MessageText
from lda.shared.kernel.errors import DomainError
from lda.shared.kernel.ids import TenantId


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    """A user message has text and a client id; an assistant message has a record id and neither.

    An assistant message can never carry answer text, quoted excerpts or a client id: the shape is
    refused here and by a database CHECK. The record is a content-free provenance row owned by
    `citations`; it does not contain the answer, so the answer cannot be rebuilt from it.
    """

    tenant_id: TenantId
    conversation_id: ConversationId
    sequence: int
    role: MessageRole
    created_at: datetime
    client_message_id: ClientMessageId | None = None
    text: MessageText | None = None
    generation_record_id: UUID | None = None

    def __post_init__(self) -> None:
        user_shape = (
            self.client_message_id is not None
            and self.text is not None
            and self.generation_record_id is None
        )
        assistant_shape = (
            self.generation_record_id is not None
            and self.text is None
            and self.client_message_id is None
        )
        expected = user_shape if self.role is MessageRole.USER else assistant_shape
        if (
            not expected
            or not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or not 1 <= self.sequence <= HARD_MAX_MESSAGES
            or self.created_at.tzinfo is None
        ):
            raise DomainError(codes.INVALID)

    @classmethod
    def user(
        cls,
        *,
        tenant_id: TenantId,
        conversation_id: ConversationId,
        sequence: int,
        client_message_id: ClientMessageId,
        text: MessageText,
        now: datetime,
    ) -> Message:
        return cls(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            sequence=sequence,
            role=MessageRole.USER,
            created_at=now,
            client_message_id=client_message_id,
            text=text,
        )

    @classmethod
    def assistant(
        cls,
        *,
        tenant_id: TenantId,
        conversation_id: ConversationId,
        sequence: int,
        generation_record_id: UUID,
        now: datetime,
    ) -> Message:
        return cls(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            sequence=sequence,
            role=MessageRole.ASSISTANT,
            created_at=now,
            generation_record_id=generation_record_id,
        )
