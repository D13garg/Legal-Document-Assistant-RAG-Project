"""The conversation aggregate root: identity, owner and the message counter."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.ids import ConversationId
from lda.shared.kernel.errors import DomainError
from lda.shared.kernel.ids import PrincipalId, TenantId

HARD_MAX_MESSAGES = 1_000
"""Absolute ceiling, equal to the database CHECK: it bounds deletion to one transaction."""


@dataclass(frozen=True, slots=True)
class Conversation:
    """Owned by exactly one principal of one tenant for its whole life; never reassigned."""

    id: ConversationId
    tenant_id: TenantId
    owner: PrincipalId
    created_at: datetime
    message_count: int = 0

    def __post_init__(self) -> None:
        count = self.message_count
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 0 <= count <= HARD_MAX_MESSAGES
            or self.created_at.tzinfo is None
        ):
            raise DomainError(codes.INVALID)

    @classmethod
    def start(
        cls, *, id: ConversationId, tenant_id: TenantId, owner: PrincipalId, now: datetime
    ) -> Conversation:
        return cls(id=id, tenant_id=tenant_id, owner=owner, created_at=now)

    def owned_by(self, principal: PrincipalId) -> bool:
        return self.owner == principal

    def has_room(self, max_messages: int) -> bool:
        return self.message_count < min(max_messages, HARD_MAX_MESSAGES)

    @property
    def next_sequence(self) -> int:
        return self.message_count + 1

    def advanced(self) -> Conversation:
        """The same conversation after one more message was appended."""
        return replace(self, message_count=self.message_count + 1)
