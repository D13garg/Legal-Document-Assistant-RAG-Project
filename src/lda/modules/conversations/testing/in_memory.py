"""In-memory conversations adapters with the same observable behaviour as the SQL ones.

Writes are staged and reach the committed store only on commit (with the audit facts), so rollback,
atomicity and tenant/owner isolation can be tested without a database. A contract suite runs the
same scenarios against both implementations. Never imported by production code.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self
from uuid import UUID

from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.conversation import Conversation
from lda.modules.conversations.domain.ids import ClientMessageId, ConversationId
from lda.modules.conversations.domain.message import Message
from lda.modules.conversations.domain.receipt import DeletionReceipt
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import JobId, PrincipalId, TenantId
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobSpec

CKey = tuple[TenantId, ConversationId]


class InMemoryConversationsStore:
    """The committed state, shared by every unit of work of one test."""

    def __init__(self) -> None:
        self.conversations: dict[CKey, Conversation] = {}
        self.messages: dict[CKey, list[Message]] = {}
        self.receipts: dict[CKey, DeletionReceipt] = {}
        self.audit_facts: list[AuditFact] = []
        self.version = 0


class _Recorder:
    """Audit/events/jobs sink of one unit of work; `fail_audit` simulates a failing audit write."""

    def __init__(self, fail_audit: bool) -> None:
        self.facts: list[AuditFact] = []
        self._fail_audit = fail_audit

    def record(self, fact: AuditFact) -> None:
        if self._fail_audit:
            raise InfrastructureError("audit_unavailable")
        self.facts.append(fact)

    def publish(self, event: DomainEvent, delivery: Delivery) -> None:  # pragma: no cover
        raise AssertionError("conversations publishes no events")

    def enqueue(self, spec: JobSpec) -> JobId | None:  # pragma: no cover
        raise AssertionError("conversations enqueues no jobs")


class InMemoryConversationRepository:
    def __init__(
        self,
        tenant_id: TenantId,
        conversations: dict[CKey, Conversation],
        messages: dict[CKey, list[Message]],
    ) -> None:
        self._tenant_id = tenant_id
        self._conversations = conversations
        self._messages = messages

    def _same_tenant(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tenant_id:
            raise InfrastructureError(codes.TENANT_MISMATCH)

    def add(self, conversation: Conversation) -> None:
        self._same_tenant(conversation.tenant_id)
        if any(cid == conversation.id for _, cid in self._conversations):
            raise ConflictError(codes.EXISTS)
        key = (self._tenant_id, conversation.id)
        self._conversations[key] = conversation
        self._messages[key] = []

    def get(self, conversation_id: ConversationId, owner: PrincipalId) -> Conversation | None:
        found = self._conversations.get((self._tenant_id, conversation_id))
        return found if found is not None and found.owned_by(owner) else None

    def get_for_update(
        self, conversation_id: ConversationId, owner: PrincipalId
    ) -> Conversation | None:
        return self.get(conversation_id, owner)

    def list_recent(self, owner: PrincipalId, limit: int) -> tuple[Conversation, ...]:
        mine = [
            c
            for (t, _), c in self._conversations.items()
            if t == self._tenant_id and c.owned_by(owner)
        ]
        mine.sort(key=lambda c: (c.created_at, c.id.value), reverse=True)
        return tuple(mine[:limit])

    def messages(self, conversation: Conversation) -> tuple[Message, ...]:
        self._same_tenant(conversation.tenant_id)
        return tuple(self._messages.get((self._tenant_id, conversation.id), ()))

    def _all(self) -> list[Message]:
        return [m for (t, _), ms in self._messages.items() if t == self._tenant_id for m in ms]

    def find_user_message(
        self, conversation: Conversation, client_message_id: ClientMessageId
    ) -> Message | None:
        for message in self._messages.get((self._tenant_id, conversation.id), ()):
            if message.client_message_id == client_message_id:
                return message
        return None

    def find_by_record(self, record_id: UUID) -> Message | None:
        for message in self._all():
            if message.generation_record_id == record_id:
                return message
        return None

    def append(self, advanced: Conversation, message: Message) -> None:
        self._same_tenant(advanced.tenant_id)
        self._same_tenant(message.tenant_id)
        key = (self._tenant_id, advanced.id)
        current = self._conversations.get(key)
        if message.conversation_id != advanced.id or message.sequence != advanced.message_count:
            raise InfrastructureError(codes.CORRUPT)
        if current is None or current.message_count != advanced.message_count - 1:
            raise ConflictError(codes.STATE_CHANGED)
        if message.client_message_id is not None and self.find_user_message(
            current, message.client_message_id
        ):
            raise ConflictError(codes.MESSAGE_CONFLICT)
        if message.generation_record_id is not None and self.find_by_record(
            message.generation_record_id
        ):
            raise ConflictError(codes.RECORD_LINKED)
        self._conversations[key] = advanced
        self._messages[key] = [*self._messages[key], message]

    def delete(self, conversation: Conversation) -> int:
        self._same_tenant(conversation.tenant_id)
        key = (self._tenant_id, conversation.id)
        removed = len(self._messages.pop(key, []))
        del self._conversations[key]
        return removed


class InMemoryDeletionReceiptRepository:
    def __init__(self, tenant_id: TenantId, receipts: dict[CKey, DeletionReceipt]) -> None:
        self._tenant_id = tenant_id
        self._receipts = receipts

    def add(self, receipt: DeletionReceipt) -> None:
        if receipt.tenant_id != self._tenant_id:
            raise InfrastructureError(codes.TENANT_MISMATCH)
        key = (self._tenant_id, receipt.conversation_id)
        if key in self._receipts:
            raise InfrastructureError(codes.PERSISTENCE_FAILED)
        self._receipts[key] = receipt


class InMemoryConversationsUnitOfWork:
    def __init__(
        self, tenant_id: TenantId, store: InMemoryConversationsStore, fail_audit: bool
    ) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self._base_version = store.version
        self._conversations = dict(store.conversations)
        self._messages = {k: list(v) for k, v in store.messages.items()}
        self._receipts = dict(store.receipts)
        self._shared = _Recorder(fail_audit)
        self._repo = InMemoryConversationRepository(tenant_id, self._conversations, self._messages)
        self._receipt_repo = InMemoryDeletionReceiptRepository(tenant_id, self._receipts)
        self.committed = False
        self.rolled_back = False

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    @property
    def events(self) -> _Recorder:
        return self._shared

    @property
    def jobs(self) -> _Recorder:
        return self._shared

    @property
    def audit(self) -> _Recorder:
        return self._shared

    @property
    def conversations(self) -> InMemoryConversationRepository:
        return self._repo

    @property
    def receipts(self) -> InMemoryDeletionReceiptRepository:
        return self._receipt_repo

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.committed:
            self.rolled_back = True

    def commit(self) -> None:
        if self._store.version != self._base_version:
            raise ConflictError(codes.STATE_CHANGED)  # someone committed first
        self._store.conversations = self._conversations
        self._store.messages = self._messages
        self._store.receipts = self._receipts
        self._store.audit_facts.extend(self._shared.facts)
        self._store.version += 1
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class InMemoryConversationsUnitOfWorkFactory:
    def __init__(self, store: InMemoryConversationsStore | None = None) -> None:
        self.store = store or InMemoryConversationsStore()
        self.fail_audit = False
        self.begun: list[InMemoryConversationsUnitOfWork] = []

    def begin(self, ctx: ExecutionContext) -> InMemoryConversationsUnitOfWork:
        uow = InMemoryConversationsUnitOfWork(ctx.tenant_id, self.store, self.fail_audit)
        self.begun.append(uow)
        return uow
