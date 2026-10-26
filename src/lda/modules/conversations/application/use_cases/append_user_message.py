from __future__ import annotations

from lda.modules.conversations.application.attribution import attribution_of, require_principal
from lda.modules.conversations.application.dto import (
    AppendedMessageView,
    AppendUserMessageCommand,
    message_view,
)
from lda.modules.conversations.application.limits import ConversationLimits
from lda.modules.conversations.application.ports import ConversationsUnitOfWorkFactory
from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.message import Message
from lda.modules.conversations.domain.text import MessageText
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import ConflictError, DomainError, NotFoundError, ValidationError
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock


@requires("conversations.append_user_message")
@audited("conversations.user_message_added")
class AppendUserMessage:
    """Append the owner's message. Idempotent per `client_message_id` (ADR-039 C6).

    The same id with identical text returns the original message (`replayed=True`, no write, no
    audit fact); the same id with different text is `conversation_message_conflict`, which never
    includes either text.
    """

    def __init__(
        self,
        units_of_work: ConversationsUnitOfWorkFactory,
        clock: Clock,
        limits: ConversationLimits,
    ) -> None:
        self._units_of_work = units_of_work
        self._clock = clock
        self._limits = limits

    def execute(
        self, ctx: ExecutionContext, command: AppendUserMessageCommand
    ) -> AppendedMessageView:
        caller = require_principal(ctx)
        try:
            text = MessageText.within(command.text, self._limits.max_text_chars)
        except DomainError as exc:
            raise ValidationError(exc.code) from None
        with self._units_of_work.begin(caller) as uow:
            conversation = uow.conversations.get_for_update(
                command.conversation_id, caller.principal_id
            )
            if conversation is None:
                raise NotFoundError(codes.NOT_FOUND)
            existing = uow.conversations.find_user_message(conversation, command.client_message_id)
            if existing is not None:
                if existing.text != text:
                    raise ConflictError(codes.MESSAGE_CONFLICT)
                return AppendedMessageView(message_view(existing), replayed=True)
            if not conversation.has_room(self._limits.max_messages):
                raise ConflictError(codes.LIMIT_REACHED)
            message = Message.user(
                tenant_id=caller.tenant_id,
                conversation_id=conversation.id,
                sequence=conversation.next_sequence,
                client_message_id=command.client_message_id,
                text=text,
                now=self._clock.now(),
            )
            uow.conversations.append(conversation.advanced(), message)
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="conversations.user_message_added",
                    resource_type="conversation",
                    resource_id=str(conversation.id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=message.created_at,
                    attributes={**who.attributes, "sequence": str(message.sequence)},
                )
            )
            uow.commit()
        return AppendedMessageView(message_view(message), replayed=False)
