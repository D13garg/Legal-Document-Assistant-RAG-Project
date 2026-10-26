from __future__ import annotations

from lda.modules.conversations.application.attribution import require_principal
from lda.modules.conversations.application.dto import (
    ConversationHistoryView,
    ConversationReference,
    conversation_view,
    message_view,
)
from lda.modules.conversations.application.ports import ConversationsUnitOfWorkFactory
from lda.modules.conversations.domain import codes
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import not_audited, requires
from lda.shared.kernel.errors import NotFoundError


@requires("conversations.read_conversation")
@not_audited(reason="Read-only owner query; reads are not audited (ADR-019 D2).")
class ReadConversation:
    """The owner's conversation with all its messages (bounded by the message limit), in order.

    Assistant messages show their record id and no text: the answer is not stored (ADR-039 C1).
    """

    def __init__(self, units_of_work: ConversationsUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def execute(
        self, ctx: ExecutionContext, query: ConversationReference
    ) -> ConversationHistoryView:
        caller = require_principal(ctx)
        with self._units_of_work.begin(caller) as uow:
            conversation = uow.conversations.get(query.conversation_id, caller.principal_id)
            if conversation is None:
                raise NotFoundError(codes.NOT_FOUND)
            messages = uow.conversations.messages(conversation)
        return ConversationHistoryView(
            conversation_view(conversation), tuple(message_view(m) for m in messages)
        )
