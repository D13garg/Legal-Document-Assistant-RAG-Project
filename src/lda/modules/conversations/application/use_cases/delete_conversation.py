from __future__ import annotations

from lda.modules.conversations.application.attribution import attribution_of, require_principal
from lda.modules.conversations.application.dto import ConversationReference, DeletionView
from lda.modules.conversations.application.ports import ConversationsUnitOfWorkFactory
from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.receipt import DELETION_SCOPE, DeletionReceipt
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import InfrastructureError, NotFoundError
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock


@requires("conversations.delete_conversation")
@audited("conversations.conversation_deleted")
class DeleteConversation:
    """Hard-delete the owner's conversation and every message in one bounded transaction.

    The conversation row, its messages, the deletion receipt and the audit fact commit together or
    not at all. Deleting touches nothing outside this module: generation records, audit history and
    principal records are unchanged. It is not time-based retention and not comprehensive erasure.
    A repeated delete finds nothing and fails as `conversation_not_found` like any absent or
    not-owned conversation (an unobservable distinction is the point, ADR-039 C4).
    """

    def __init__(self, units_of_work: ConversationsUnitOfWorkFactory, clock: Clock) -> None:
        self._units_of_work = units_of_work
        self._clock = clock

    def execute(self, ctx: ExecutionContext, command: ConversationReference) -> DeletionView:
        caller = require_principal(ctx)
        with self._units_of_work.begin(caller) as uow:
            conversation = uow.conversations.get_for_update(
                command.conversation_id, caller.principal_id
            )
            if conversation is None:
                raise NotFoundError(codes.NOT_FOUND)
            removed = uow.conversations.delete(conversation)
            if removed != conversation.message_count:
                raise InfrastructureError(codes.CORRUPT)  # rolls everything back
            now = self._clock.now()
            uow.receipts.add(DeletionReceipt(caller.tenant_id, conversation.id, now, removed))
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="conversations.conversation_deleted",
                    resource_type="conversation",
                    resource_id=str(conversation.id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=now,
                    attributes={**who.attributes, "messages_deleted": str(removed)},
                )
            )
            uow.commit()
        return DeletionView(conversation.id, removed, DELETION_SCOPE)
