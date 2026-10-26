"""Public API of conversations (ADR-039): an owner-scoped, immutable, append-only message log.

It stores user messages and, for assistant turns, only a reference to a `GenerationRecord`. It
generates no answers and holds no document or version reference.
"""

from lda.modules.conversations.application.dto import (
    AppendAssistantTurnCommand,
    AppendedMessageView,
    AppendUserMessageCommand,
    ConversationHistoryView,
    ConversationListView,
    ConversationReference,
    ConversationView,
    DeletionView,
    ListConversationsQuery,
    MessageView,
    StartConversationCommand,
)
from lda.modules.conversations.application.limits import ConversationLimits
from lda.modules.conversations.application.ports import (
    ConversationsUnitOfWork,
    ConversationsUnitOfWorkFactory,
)
from lda.modules.conversations.application.queries.list_conversations import ListConversations
from lda.modules.conversations.application.queries.read_conversation import ReadConversation
from lda.modules.conversations.application.use_cases.append_assistant_turn import (
    AppendAssistantTurn,
)
from lda.modules.conversations.application.use_cases.append_user_message import AppendUserMessage
from lda.modules.conversations.application.use_cases.delete_conversation import DeleteConversation
from lda.modules.conversations.application.use_cases.start_conversation import StartConversation
from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.ids import ClientMessageId, ConversationId
from lda.modules.conversations.domain.message import MessageRole
from lda.modules.conversations.domain.receipt import DELETION_SCOPE

NOT_FOUND = codes.NOT_FOUND
MESSAGE_INVALID = codes.MESSAGE_INVALID
MESSAGE_CONFLICT = codes.MESSAGE_CONFLICT
LIMIT_REACHED = codes.LIMIT_REACHED
RECORD_UNAVAILABLE = codes.RECORD_UNAVAILABLE
RECORD_LINKED = codes.RECORD_LINKED

__all__ = [
    "DELETION_SCOPE",
    "LIMIT_REACHED",
    "MESSAGE_CONFLICT",
    "MESSAGE_INVALID",
    "NOT_FOUND",
    "RECORD_LINKED",
    "RECORD_UNAVAILABLE",
    "AppendAssistantTurn",
    "AppendAssistantTurnCommand",
    "AppendUserMessage",
    "AppendUserMessageCommand",
    "AppendedMessageView",
    "ClientMessageId",
    "ConversationHistoryView",
    "ConversationId",
    "ConversationLimits",
    "ConversationListView",
    "ConversationReference",
    "ConversationView",
    "ConversationsUnitOfWork",
    "ConversationsUnitOfWorkFactory",
    "DeleteConversation",
    "DeletionView",
    "ListConversations",
    "ListConversationsQuery",
    "MessageRole",
    "MessageView",
    "ReadConversation",
    "StartConversation",
    "StartConversationCommand",
]
