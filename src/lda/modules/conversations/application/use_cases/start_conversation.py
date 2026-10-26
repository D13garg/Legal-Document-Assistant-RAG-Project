from __future__ import annotations

from lda.modules.conversations.application.attribution import attribution_of, require_principal
from lda.modules.conversations.application.dto import (
    ConversationView,
    StartConversationCommand,
    conversation_view,
)
from lda.modules.conversations.application.ports import ConversationsUnitOfWorkFactory
from lda.modules.conversations.domain.conversation import Conversation
from lda.modules.conversations.domain.ids import ConversationId
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock, IdGenerator


@requires("conversations.start_conversation")
@audited("conversations.conversation_started")
class StartConversation:
    """Start an empty conversation owned by the calling principal.

    Not idempotent by design: each call creates a new conversation (ADR-039 C6 only makes message
    appends idempotent).
    """

    def __init__(
        self, units_of_work: ConversationsUnitOfWorkFactory, clock: Clock, ids: IdGenerator
    ) -> None:
        self._units_of_work = units_of_work
        self._clock = clock
        self._ids = ids

    def execute(self, ctx: ExecutionContext, command: StartConversationCommand) -> ConversationView:
        caller = require_principal(ctx)
        conversation = Conversation.start(
            id=ConversationId(self._ids.new_uuid()),
            tenant_id=caller.tenant_id,
            owner=caller.principal_id,
            now=self._clock.now(),
        )
        with self._units_of_work.begin(caller) as uow:
            uow.conversations.add(conversation)
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="conversations.conversation_started",
                    resource_type="conversation",
                    resource_id=str(conversation.id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=conversation.created_at,
                    attributes=who.attributes,
                )
            )
            uow.commit()
        return conversation_view(conversation)
