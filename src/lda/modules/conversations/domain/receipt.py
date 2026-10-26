"""The content-free evidence that a conversation was deleted (ADR-039 C5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.conversation import HARD_MAX_MESSAGES
from lda.modules.conversations.domain.ids import ConversationId
from lda.shared.kernel.errors import DomainError
from lda.shared.kernel.ids import TenantId

DELETION_SCOPE = "conversations.messages_and_rows"
"""All a receipt claims: the conversation row and its message rows were removed. It says nothing
about generation records, audit history, identity records, backups or any other store."""


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    """Identifiers, a time and a count. No principal, title, text or message content."""

    tenant_id: TenantId
    conversation_id: ConversationId
    deleted_at: datetime
    messages_deleted: int
    scope: str = DELETION_SCOPE

    def __post_init__(self) -> None:
        if (
            self.scope != DELETION_SCOPE
            or not isinstance(self.messages_deleted, int)
            or isinstance(self.messages_deleted, bool)
            or not 0 <= self.messages_deleted <= HARD_MAX_MESSAGES
            or self.deleted_at.tzinfo is None
        ):
            raise DomainError(codes.INVALID)
