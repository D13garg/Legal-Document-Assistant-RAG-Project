"""Ports the use cases need. Adapters: infrastructure (SQL) and testing (in-memory)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from lda.modules.conversations.domain.conversation import Conversation
from lda.modules.conversations.domain.ids import ClientMessageId, ConversationId
from lda.modules.conversations.domain.message import Message
from lda.modules.conversations.domain.receipt import DeletionReceipt
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.ids import PrincipalId
from lda.shared.ports.unit_of_work import UnitOfWork


class ConversationRepository(Protocol):
    """Bound to one tenant for its whole life. Every read of a conversation names its owner."""

    def add(self, conversation: Conversation) -> None:
        """Insert. Raises ConflictError('conversation_exists') on a duplicate id."""
        ...

    def get(self, conversation_id: ConversationId, owner: PrincipalId) -> Conversation | None:
        """The owner's conversation of the bound tenant; otherwise None (never says why)."""
        ...

    def get_for_update(
        self, conversation_id: ConversationId, owner: PrincipalId
    ) -> Conversation | None:
        """Like `get`, and serializes concurrent appends and deletes of this conversation."""
        ...

    def list_recent(self, owner: PrincipalId, limit: int) -> tuple[Conversation, ...]:
        """At most `limit` conversations of the owner, newest first."""
        ...

    def messages(self, conversation: Conversation) -> tuple[Message, ...]:
        """The messages of a conversation the caller already proved to own, by sequence."""
        ...

    def find_user_message(
        self, conversation: Conversation, client_message_id: ClientMessageId
    ) -> Message | None: ...

    def find_by_record(self, record_id: UUID) -> Message | None:
        """The assistant message that already links this record, in any conversation."""
        ...

    def append(self, advanced: Conversation, message: Message) -> None:
        """Insert the message and move the counter to `advanced.message_count` (one step).

        Raises ConflictError('conversation_state_changed') if the counter was not the previous
        value, ConflictError('conversation_message_conflict' | 'conversation_record_linked') on a
        uniqueness violation.
        """
        ...

    def delete(self, conversation: Conversation) -> int:
        """Remove the conversation and every message; return the number of messages removed."""
        ...


class DeletionReceiptRepository(Protocol):
    def add(self, receipt: DeletionReceipt) -> None:
        """Insert-only. There is no read, update or delete."""
        ...


class ConversationsUnitOfWork(UnitOfWork, Protocol):
    @property
    def conversations(self) -> ConversationRepository: ...

    @property
    def receipts(self) -> DeletionReceiptRepository: ...


class ConversationsUnitOfWorkFactory(Protocol):
    def begin(self, ctx: ExecutionContext) -> ConversationsUnitOfWork: ...
