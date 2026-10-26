"""Builders shared by the conversations tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from lda.modules.citations.api import GenerationOutcome, GenerationRecord
from lda.modules.citations.testing import InMemoryGenerationRecords
from lda.modules.conversations.api import (
    AppendAssistantTurn,
    AppendUserMessage,
    ConversationLimits,
    DeleteConversation,
    ListConversations,
    ReadConversation,
    StartConversation,
)
from lda.modules.conversations.application.ports import ConversationsUnitOfWorkFactory
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId

START = datetime(2026, 1, 1, tzinfo=UTC)
LIMITS = ConversationLimits(max_messages=4, max_text_chars=60, max_list=3)
PRIVATE_TEXT = "CONFIDENTIAL-CLIENT-MATTER-7731"


def new_tenant() -> TenantId:
    return TenantId(uuid4())


def new_principal() -> PrincipalId:
    return PrincipalId(uuid4())


def ctx_for(tenant: TenantId, principal: PrincipalId) -> TenantContext:
    return TenantContext(tenant, principal, RequestId(uuid4()))


def a_record(
    tenant: TenantId, principal: PrincipalId, record_id: UUID | None = None
) -> GenerationRecord:
    return GenerationRecord(
        record_id=record_id or uuid4(),
        tenant_id=tenant,
        principal_id=principal,
        request_id=RequestId(uuid4()),
        created_at=START,
        config_id="rag-" + "a" * 64,
        outcome=GenerationOutcome.INSUFFICIENT_EVIDENCE,
        template_id="grounded_answer",
        template_version="v1",
        model_id="fake-model:1",
        request_sha256="b" * 64,
        evidence_fingerprint="c" * 64,
        citations=(),
    )


@dataclass
class Kit:
    """Every use case wired to one unit-of-work factory (memory or SQL) and in-memory records."""

    factory: ConversationsUnitOfWorkFactory
    records: InMemoryGenerationRecords
    clock: FakeClock
    ids: SequentialIdGenerator
    start: StartConversation
    append_user: AppendUserMessage
    append_assistant: AppendAssistantTurn
    read: ReadConversation
    listing: ListConversations
    delete: DeleteConversation
    tenant: TenantId
    owner: PrincipalId
    ctx: TenantContext

    def other_principal(self) -> TenantContext:
        return ctx_for(self.tenant, new_principal())

    def other_tenant(self) -> TenantContext:
        return ctx_for(new_tenant(), self.owner)


def build_kit(factory: ConversationsUnitOfWorkFactory, limits: ConversationLimits = LIMITS) -> Kit:
    records = InMemoryGenerationRecords()
    clock = FakeClock(START)
    ids = SequentialIdGenerator()
    tenant, owner = new_tenant(), new_principal()
    return Kit(
        factory=factory,
        records=records,
        clock=clock,
        ids=ids,
        start=StartConversation(factory, clock, ids),
        append_user=AppendUserMessage(factory, clock, limits),
        append_assistant=AppendAssistantTurn(factory, records, clock, limits),
        read=ReadConversation(factory),
        listing=ListConversations(factory, limits),
        delete=DeleteConversation(factory, clock),
        tenant=tenant,
        owner=owner,
        ctx=ctx_for(tenant, owner),
    )
