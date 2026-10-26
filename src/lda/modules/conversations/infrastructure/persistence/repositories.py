"""Tenant-bound SQL repositories. Every statement carries the tenant predicate and RLS enforces it
again. Driver errors are translated and chained away: they embed statement parameters, which would
put message text into logs."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.conversation import Conversation
from lda.modules.conversations.domain.ids import ClientMessageId, ConversationId
from lda.modules.conversations.domain.message import Message, MessageRole
from lda.modules.conversations.domain.receipt import DeletionReceipt
from lda.modules.conversations.domain.text import MessageText
from lda.modules.conversations.infrastructure.persistence.tables import (
    conversations,
    deletion_receipts,
    messages,
)
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import ConflictError, DomainError, InfrastructureError
from lda.shared.kernel.ids import PrincipalId

_UNIQUE_VIOLATION = "23505"
_RECORD_UNIQUE = "messages_record_unique"


def _failed() -> InfrastructureError:
    return InfrastructureError(codes.PERSISTENCE_FAILED)


class SqlConversationRepository:
    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    # ---- conversations -------------------------------------------------------------------------
    def add(self, conversation: Conversation) -> None:
        self._same_tenant(conversation.tenant_id.value)
        try:
            self._tx.session.execute(
                insert(conversations).values(
                    tenant_id=conversation.tenant_id.value,
                    id=conversation.id.value,
                    owner_id=conversation.owner.value,
                    created_at=conversation.created_at,
                    message_count=conversation.message_count,
                )
            )
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                raise ConflictError(codes.EXISTS) from None
            raise _failed() from None
        except SQLAlchemyError:
            raise _failed() from None

    def get(self, conversation_id: ConversationId, owner: PrincipalId) -> Conversation | None:
        return self._load(conversation_id, owner, lock=False)

    def get_for_update(
        self, conversation_id: ConversationId, owner: PrincipalId
    ) -> Conversation | None:
        return self._load(conversation_id, owner, lock=True)

    def list_recent(self, owner: PrincipalId, limit: int) -> tuple[Conversation, ...]:
        statement = (
            select(conversations)
            .where(
                conversations.c.tenant_id == self._tx.tenant_id.value,
                conversations.c.owner_id == owner.value,
            )
            .order_by(conversations.c.created_at.desc(), conversations.c.id.desc())
            .limit(limit)
        )
        try:
            rows = self._tx.session.execute(statement).mappings().all()
        except SQLAlchemyError:
            raise _failed() from None
        return tuple(self._conversation(row) for row in rows)

    def _load(
        self, conversation_id: ConversationId, owner: PrincipalId, *, lock: bool
    ) -> Conversation | None:
        statement = select(conversations).where(
            conversations.c.tenant_id == self._tx.tenant_id.value,
            conversations.c.id == conversation_id.value,
            conversations.c.owner_id == owner.value,
        )
        if lock:
            statement = statement.with_for_update()
        try:
            row = self._tx.session.execute(statement).mappings().one_or_none()
        except SQLAlchemyError:
            raise _failed() from None
        return None if row is None else self._conversation(row)

    # ---- messages ------------------------------------------------------------------------------
    def messages(self, conversation: Conversation) -> tuple[Message, ...]:
        self._same_tenant(conversation.tenant_id.value)
        statement = (
            select(messages)
            .where(
                messages.c.tenant_id == self._tx.tenant_id.value,
                messages.c.conversation_id == conversation.id.value,
            )
            .order_by(messages.c.sequence)
        )
        try:
            rows = self._tx.session.execute(statement).mappings().all()
        except SQLAlchemyError:
            raise _failed() from None
        found = tuple(self._message(row) for row in rows)
        if [m.sequence for m in found] != list(range(1, conversation.message_count + 1)):
            raise InfrastructureError(codes.CORRUPT)
        return found

    def find_user_message(
        self, conversation: Conversation, client_message_id: ClientMessageId
    ) -> Message | None:
        return self._one(
            messages.c.conversation_id == conversation.id.value,
            messages.c.client_message_id == client_message_id.value,
        )

    def find_by_record(self, record_id: UUID) -> Message | None:
        return self._one(messages.c.record_id == record_id)

    def _one(self, *conditions: Any) -> Message | None:
        statement = select(messages).where(
            messages.c.tenant_id == self._tx.tenant_id.value, *conditions
        )
        try:
            row = self._tx.session.execute(statement).mappings().one_or_none()
        except SQLAlchemyError:
            raise _failed() from None
        return None if row is None else self._message(row)

    def append(self, advanced: Conversation, message: Message) -> None:
        self._same_tenant(advanced.tenant_id.value)
        self._same_tenant(message.tenant_id.value)
        if message.conversation_id != advanced.id or message.sequence != advanced.message_count:
            raise InfrastructureError(codes.CORRUPT)
        try:
            changed = self._tx.execute_count(
                update(conversations)
                .where(
                    conversations.c.tenant_id == advanced.tenant_id.value,
                    conversations.c.id == advanced.id.value,
                    conversations.c.message_count == advanced.message_count - 1,
                )
                .values(message_count=advanced.message_count),
                {},
            )
        except SQLAlchemyError:
            raise _failed() from None
        if changed != 1:
            raise ConflictError(codes.STATE_CHANGED)
        try:
            self._tx.session.execute(
                insert(messages).values(
                    tenant_id=message.tenant_id.value,
                    conversation_id=message.conversation_id.value,
                    sequence=message.sequence,
                    role=message.role.value,
                    created_at=message.created_at,
                    client_message_id=(
                        None
                        if message.client_message_id is None
                        else message.client_message_id.value
                    ),
                    text=None if message.text is None else message.text.value,
                    record_id=message.generation_record_id,
                )
            )
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) != _UNIQUE_VIOLATION:
                raise _failed() from None
            diag = getattr(exc.orig, "diag", None)
            if getattr(diag, "constraint_name", None) == _RECORD_UNIQUE:
                raise ConflictError(codes.RECORD_LINKED) from None
            raise ConflictError(codes.MESSAGE_CONFLICT) from None
        except SQLAlchemyError:
            raise _failed() from None

    def delete(self, conversation: Conversation) -> int:
        """Delete the conversation row; the database removes its messages with it (the only way a
        message can be deleted). Verifies that nothing of it remains."""
        self._same_tenant(conversation.tenant_id.value)
        tenant, cid = conversation.tenant_id.value, conversation.id.value
        try:
            before = self._count(cid)
            changed = self._tx.execute_count(
                delete(conversations).where(
                    conversations.c.tenant_id == tenant,
                    conversations.c.id == cid,
                    conversations.c.owner_id == conversation.owner.value,
                ),
                {},
            )
            remaining = self._count(cid)
        except SQLAlchemyError:
            raise _failed() from None
        if changed != 1 or remaining != 0:
            raise InfrastructureError(codes.CORRUPT)
        return before

    def _count(self, conversation_id: UUID) -> int:
        return int(
            self._tx.session.execute(
                select(func.count()).where(
                    messages.c.tenant_id == self._tx.tenant_id.value,
                    messages.c.conversation_id == conversation_id,
                )
            ).scalar_one()
        )

    # ---- row mapping ---------------------------------------------------------------------------
    def _same_tenant(self, tenant: UUID) -> None:
        if tenant != self._tx.tenant_id.value:
            raise InfrastructureError(codes.TENANT_MISMATCH)

    def _conversation(self, row: RowMapping) -> Conversation:
        try:
            return Conversation(
                id=ConversationId(row["id"]),
                tenant_id=self._tx.tenant_id,
                owner=PrincipalId(row["owner_id"]),
                created_at=row["created_at"],
                message_count=row["message_count"],
            )
        except DomainError:
            raise InfrastructureError(codes.CORRUPT) from None

    def _message(self, row: RowMapping) -> Message:
        try:
            return Message(
                tenant_id=self._tx.tenant_id,
                conversation_id=ConversationId(row["conversation_id"]),
                sequence=row["sequence"],
                role=MessageRole(row["role"]),
                created_at=row["created_at"],
                client_message_id=(
                    None
                    if row["client_message_id"] is None
                    else ClientMessageId(row["client_message_id"])
                ),
                text=None if row["text"] is None else MessageText(row["text"]),
                generation_record_id=row["record_id"],
            )
        except (DomainError, ValueError):
            raise InfrastructureError(codes.CORRUPT) from None


class SqlDeletionReceiptRepository:
    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    def add(self, receipt: DeletionReceipt) -> None:
        if receipt.tenant_id != self._tx.tenant_id:
            raise InfrastructureError(codes.TENANT_MISMATCH)
        try:
            self._tx.session.execute(
                insert(deletion_receipts).values(
                    tenant_id=receipt.tenant_id.value,
                    conversation_id=receipt.conversation_id.value,
                    deleted_at=receipt.deleted_at,
                    messages_deleted=receipt.messages_deleted,
                    scope=receipt.scope,
                )
            )
        except SQLAlchemyError:
            raise _failed() from None
