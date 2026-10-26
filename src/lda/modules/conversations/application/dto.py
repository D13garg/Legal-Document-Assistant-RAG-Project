"""What crosses the module boundary: identifiers, times, counts and (for user messages) the user's
own text. Never persistence objects, never an answer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from lda.modules.conversations.domain.conversation import Conversation
from lda.modules.conversations.domain.ids import ClientMessageId, ConversationId
from lda.modules.conversations.domain.message import Message, MessageRole


@dataclass(frozen=True, slots=True)
class StartConversationCommand:
    """No parameters in Step 15 (no title: it can be added later without a breaking change)."""


@dataclass(frozen=True, slots=True)
class AppendUserMessageCommand:
    conversation_id: ConversationId
    client_message_id: ClientMessageId
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AppendAssistantTurnCommand:
    conversation_id: ConversationId
    generation_record_id: UUID


@dataclass(frozen=True, slots=True)
class ConversationReference:
    conversation_id: ConversationId


@dataclass(frozen=True, slots=True)
class ListConversationsQuery:
    limit: int = 20


@dataclass(frozen=True, slots=True)
class ConversationView:
    conversation_id: ConversationId
    created_at: datetime
    message_count: int


@dataclass(frozen=True, slots=True)
class MessageView:
    """An assistant message carries `generation_record_id` and no text. Its answer is NOT stored
    by this module and cannot be reconstructed from the generation record, which is content-free
    provenance (ids, hashes, labels, citations): history shows that a turn happened and which
    record documents it, not what was said (ADR-039 C1)."""

    conversation_id: ConversationId
    sequence: int
    role: MessageRole
    created_at: datetime
    client_message_id: ClientMessageId | None = None
    text: str | None = field(default=None, repr=False)
    generation_record_id: UUID | None = None

    @property
    def text_stored(self) -> bool:
        """False for every assistant message: there is no stored answer text to show."""
        return self.text is not None


@dataclass(frozen=True, slots=True)
class AppendedMessageView:
    message: MessageView
    replayed: bool
    """True when the same client message id (or record id) had already been appended."""


@dataclass(frozen=True, slots=True)
class ConversationHistoryView:
    conversation: ConversationView
    messages: tuple[MessageView, ...]


@dataclass(frozen=True, slots=True)
class ConversationListView:
    conversations: tuple[ConversationView, ...]


@dataclass(frozen=True, slots=True)
class DeletionView:
    conversation_id: ConversationId
    messages_deleted: int
    scope: str


def conversation_view(conversation: Conversation) -> ConversationView:
    return ConversationView(conversation.id, conversation.created_at, conversation.message_count)


def message_view(message: Message) -> MessageView:
    return MessageView(
        conversation_id=message.conversation_id,
        sequence=message.sequence,
        role=message.role,
        created_at=message.created_at,
        client_message_id=message.client_message_id,
        text=None if message.text is None else message.text.value,
        generation_record_id=message.generation_record_id,
    )
