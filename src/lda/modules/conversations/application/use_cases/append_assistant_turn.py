from __future__ import annotations

from lda.modules.citations.api import GenerationRecordRepository
from lda.modules.conversations.application.attribution import attribution_of, require_principal
from lda.modules.conversations.application.dto import (
    AppendAssistantTurnCommand,
    AppendedMessageView,
    message_view,
)
from lda.modules.conversations.application.limits import ConversationLimits
from lda.modules.conversations.application.ports import ConversationsUnitOfWorkFactory
from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.message import Message
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import ConflictError, NotFoundError
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock


@requires("conversations.append_assistant_turn")
@audited("conversations.assistant_turn_added")
class AppendAssistantTurn:
    """Link a `GenerationRecord` to the owner's conversation as an assistant turn, by reference.

    Nothing but the record id is stored: no answer text, quote or excerpt (ADR-039 C1). The record
    is read through `citations.api` and must belong to the caller's tenant and principal; any
    other case (absent, foreign tenant, other principal) is one code,
    `conversation_record_unavailable`.
    It is read before the conversation is touched, so the response never depends on whether a
    conversation exists. Idempotent per record id (ADR-039 C6): the same record in the same
    conversation replays; the same record in another conversation is `conversation_record_linked`.
    """

    def __init__(
        self,
        units_of_work: ConversationsUnitOfWorkFactory,
        records: GenerationRecordRepository,
        clock: Clock,
        limits: ConversationLimits,
    ) -> None:
        self._units_of_work = units_of_work
        self._records = records
        self._clock = clock
        self._limits = limits

    def execute(
        self, ctx: ExecutionContext, command: AppendAssistantTurnCommand
    ) -> AppendedMessageView:
        caller = require_principal(ctx)
        record = self._records.get(caller.tenant_id, command.generation_record_id)
        if (
            record is None
            or record.tenant_id != caller.tenant_id
            or record.principal_id != caller.principal_id
        ):
            raise NotFoundError(codes.RECORD_UNAVAILABLE)
        with self._units_of_work.begin(caller) as uow:
            conversation = uow.conversations.get_for_update(
                command.conversation_id, caller.principal_id
            )
            if conversation is None:
                raise NotFoundError(codes.NOT_FOUND)
            existing = uow.conversations.find_by_record(record.record_id)
            if existing is not None:
                if existing.conversation_id != conversation.id:
                    raise ConflictError(codes.RECORD_LINKED)
                return AppendedMessageView(message_view(existing), replayed=True)
            if not conversation.has_room(self._limits.max_messages):
                raise ConflictError(codes.LIMIT_REACHED)
            message = Message.assistant(
                tenant_id=caller.tenant_id,
                conversation_id=conversation.id,
                sequence=conversation.next_sequence,
                generation_record_id=record.record_id,
                now=self._clock.now(),
            )
            uow.conversations.append(conversation.advanced(), message)
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="conversations.assistant_turn_added",
                    resource_type="conversation",
                    resource_id=str(conversation.id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=message.created_at,
                    attributes={
                        **who.attributes,
                        "sequence": str(message.sequence),
                        "record_id": str(record.record_id),
                        "generation_outcome": record.outcome.value,
                    },
                )
            )
            uow.commit()
        return AppendedMessageView(message_view(message), replayed=False)
