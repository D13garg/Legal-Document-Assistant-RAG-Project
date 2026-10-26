"""Audit facts, fail-closed atomicity (in-memory) and ADR-034 declarations."""

from __future__ import annotations

from uuid import uuid4

import pytest

from lda.modules.conversations.api import (
    AppendAssistantTurn,
    AppendAssistantTurnCommand,
    AppendUserMessage,
    AppendUserMessageCommand,
    ClientMessageId,
    ConversationReference,
    DeleteConversation,
    ListConversations,
    ListConversationsQuery,
    ReadConversation,
    StartConversation,
    StartConversationCommand,
)
from lda.modules.conversations.testing import InMemoryConversationsUnitOfWorkFactory
from lda.modules.conversations.tests.support import PRIVATE_TEXT, Kit, a_record, build_kit
from lda.shared.kernel.declarations import (
    Audited,
    NotAudited,
    Requires,
    audit_of,
    authorization_of,
)
from lda.shared.kernel.errors import ConflictError, InfrastructureError, NotFoundError
from lda.shared.ports.audit import AuditFact, AuditOutcome

AUDITED = {
    StartConversation: ("conversations.start_conversation", "conversations.conversation_started"),
    AppendUserMessage: ("conversations.append_user_message", "conversations.user_message_added"),
    AppendAssistantTurn: (
        "conversations.append_assistant_turn",
        "conversations.assistant_turn_added",
    ),
    DeleteConversation: ("conversations.delete_conversation", "conversations.conversation_deleted"),
}
READS = {
    ReadConversation: "conversations.read_conversation",
    ListConversations: "conversations.list_conversations",
}


@pytest.mark.parametrize("use_case", list(AUDITED))
def test_mutations_declare_their_requirement_and_audit_action(use_case: type) -> None:
    requirement, action = AUDITED[use_case]
    assert authorization_of(use_case) == Requires((requirement,))
    assert audit_of(use_case) == Audited(action)


@pytest.mark.parametrize("use_case", list(READS))
def test_reads_declare_a_requirement_and_a_reasoned_audit_exemption(use_case: type) -> None:
    assert authorization_of(use_case) == Requires((READS[use_case],))
    declared = audit_of(use_case)
    assert isinstance(declared, NotAudited) and declared.reason


def memory_kit() -> Kit:
    return build_kit(InMemoryConversationsUnitOfWorkFactory())


def facts(kit: Kit) -> list[AuditFact]:
    store = kit.factory.store  # type: ignore[attr-defined]
    return list(store.audit_facts)


def test_every_effective_mutation_emits_exactly_one_content_free_fact() -> None:
    kit = memory_kit()
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    kit.append_user.execute(
        kit.ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), PRIVATE_TEXT)
    )
    record = a_record(kit.tenant, kit.owner)
    kit.records.add(record)
    kit.append_assistant.execute(kit.ctx, AppendAssistantTurnCommand(cid, record.record_id))
    kit.delete.execute(kit.ctx, ConversationReference(cid))
    recorded = facts(kit)
    assert [f.action for f in recorded] == [
        "conversations.conversation_started",
        "conversations.user_message_added",
        "conversations.assistant_turn_added",
        "conversations.conversation_deleted",
    ]
    assert [f.attributes for f in recorded] == [
        {},
        {"sequence": "1"},
        {
            "sequence": "2",
            "record_id": str(record.record_id),
            "generation_outcome": "insufficient_evidence",
        },
        {"messages_deleted": "2"},
    ]
    for f in recorded:
        assert (f.tenant_id, f.actor, f.resource_type, f.resource_id, f.outcome) == (
            kit.tenant,
            kit.owner,
            "conversation",
            str(cid),
            AuditOutcome.SUCCESS,
        )
        assert f.request_id == kit.ctx.request_id
        assert PRIVATE_TEXT not in repr(f)


def test_replays_failures_and_reads_emit_nothing() -> None:
    kit = memory_kit()
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    client = ClientMessageId(uuid4())
    kit.append_user.execute(kit.ctx, AppendUserMessageCommand(cid, client, "a"))
    baseline = len(facts(kit))
    kit.append_user.execute(kit.ctx, AppendUserMessageCommand(cid, client, "a"))  # replay
    with pytest.raises(ConflictError):
        kit.append_user.execute(kit.ctx, AppendUserMessageCommand(cid, client, "b"))
    with pytest.raises(NotFoundError):
        kit.read.execute(kit.other_principal(), ConversationReference(cid))
    with pytest.raises(NotFoundError):
        kit.delete.execute(kit.other_tenant(), ConversationReference(cid))
    kit.read.execute(kit.ctx, ConversationReference(cid))
    kit.listing.execute(kit.ctx, ListConversationsQuery(limit=2))
    assert len(facts(kit)) == baseline


def test_a_failing_audit_write_rolls_the_whole_action_back() -> None:
    kit = memory_kit()
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    kit.append_user.execute(kit.ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "a"))
    kit.factory.fail_audit = True  # type: ignore[attr-defined]
    with pytest.raises(InfrastructureError):
        kit.start.execute(kit.ctx, StartConversationCommand())
    with pytest.raises(InfrastructureError):
        kit.append_user.execute(
            kit.ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "b")
        )
    with pytest.raises(InfrastructureError):
        kit.delete.execute(kit.ctx, ConversationReference(cid))
    kit.factory.fail_audit = False  # type: ignore[attr-defined]
    history = kit.read.execute(kit.ctx, ConversationReference(cid))
    assert history.conversation.message_count == 1  # nothing was appended, nothing was deleted
    assert len(kit.listing.execute(kit.ctx, ListConversationsQuery(limit=3)).conversations) == 1
    assert not kit.factory.store.receipts  # type: ignore[attr-defined]
