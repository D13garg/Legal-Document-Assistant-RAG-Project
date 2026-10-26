from __future__ import annotations

from lda.modules.conversations.application.attribution import require_principal
from lda.modules.conversations.application.dto import (
    ConversationListView,
    ListConversationsQuery,
    conversation_view,
)
from lda.modules.conversations.application.limits import ConversationLimits
from lda.modules.conversations.application.ports import ConversationsUnitOfWorkFactory
from lda.modules.conversations.domain import codes
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import not_audited, requires
from lda.shared.kernel.errors import ValidationError


@requires("conversations.list_conversations")
@not_audited(reason="Read-only owner query; reads are not audited (ADR-019 D2).")
class ListConversations:
    """The caller's own most recent conversations (at most `limit`, newest first). No cursor."""

    def __init__(
        self, units_of_work: ConversationsUnitOfWorkFactory, limits: ConversationLimits
    ) -> None:
        self._units_of_work = units_of_work
        self._limits = limits

    def execute(self, ctx: ExecutionContext, query: ListConversationsQuery) -> ConversationListView:
        caller = require_principal(ctx)
        limit = query.limit
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not (1 <= limit <= self._limits.max_list)
        ):
            raise ValidationError(codes.LIMITS_INVALID)
        with self._units_of_work.begin(caller) as uow:
            found = uow.conversations.list_recent(caller.principal_id, limit)
        return ConversationListView(tuple(conversation_view(c) for c in found))
