from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError as SettingsError

from lda.modules.conversations.application.limits import HARD_MAX_LIST, ConversationLimits
from lda.modules.conversations.config import ConversationsSettings
from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.conversation import HARD_MAX_MESSAGES, Conversation
from lda.modules.conversations.domain.ids import ClientMessageId, ConversationId
from lda.modules.conversations.domain.message import Message, MessageRole
from lda.modules.conversations.domain.receipt import DELETION_SCOPE, DeletionReceipt
from lda.modules.conversations.domain.text import HARD_MAX_TEXT_CHARS, MessageText
from lda.modules.conversations.tests.support import PRIVATE_TEXT, new_principal, new_tenant
from lda.shared.kernel.errors import DomainError, ValidationError

NOW = datetime(2026, 1, 1, tzinfo=UTC)
MIGRATION = next(
    Path(__file__).parents[2].glob("infrastructure/persistence/migrations/versions/n0001_*.py")
)


# ---- MessageText ------------------------------------------------------------------------------
def test_text_is_kept_exactly_as_submitted() -> None:
    raw = "  Is clause 4.2 enforceable?\n\tSecond line  "
    assert MessageText(raw).value == raw


@pytest.mark.parametrize("bad", ["", "   ", "\n\t", "a\x00b", "x" * (HARD_MAX_TEXT_CHARS + 1)])
def test_text_refuses_blank_nul_and_oversized(bad: str) -> None:
    with pytest.raises(DomainError) as err:
        MessageText(bad)
    assert err.value.code == codes.MESSAGE_INVALID


def test_text_refuses_what_utf8_cannot_encode_and_non_strings() -> None:
    for bad in ("lone \ud800 surrogate", 5, None, b"bytes"):
        with pytest.raises(DomainError):
            MessageText(bad)  # type: ignore[arg-type]


def test_within_applies_the_configured_limit_and_never_exceeds_the_ceiling() -> None:
    assert MessageText.within("a" * 10, 10).value == "a" * 10
    with pytest.raises(DomainError):
        MessageText.within("a" * 11, 10)
    with pytest.raises(DomainError):
        MessageText.within("a" * (HARD_MAX_TEXT_CHARS + 1), 10**9)


def test_text_never_appears_in_repr_or_error() -> None:
    assert PRIVATE_TEXT not in repr(MessageText(PRIVATE_TEXT))
    with pytest.raises(DomainError) as err:
        MessageText.within(PRIVATE_TEXT, 3)
    assert (
        PRIVATE_TEXT not in repr(err.value)
        and PRIVATE_TEXT not in str(err.value) + err.value.detail
    )


# ---- Conversation -----------------------------------------------------------------------------
def make_conversation(count: int = 0) -> Conversation:
    return Conversation(ConversationId(uuid4()), new_tenant(), new_principal(), NOW, count)


def test_a_started_conversation_is_empty_and_owned_by_its_creator() -> None:
    owner = new_principal()
    c = Conversation.start(id=ConversationId(uuid4()), tenant_id=new_tenant(), owner=owner, now=NOW)
    assert c.message_count == 0 and c.next_sequence == 1
    assert c.owned_by(owner) and not c.owned_by(new_principal())


def test_advancing_counts_one_message_and_respects_the_limit() -> None:
    c = make_conversation(2)
    assert c.advanced().message_count == 3 and c.advanced().next_sequence == 4
    assert c.has_room(3) and not c.has_room(2)
    assert not make_conversation(HARD_MAX_MESSAGES).has_room(10**9)  # the ceiling always wins


@pytest.mark.parametrize("count", [-1, HARD_MAX_MESSAGES + 1, True, 1.5])
def test_conversation_refuses_an_impossible_counter(count: object) -> None:
    with pytest.raises(DomainError):
        Conversation(ConversationId(uuid4()), new_tenant(), new_principal(), NOW, count)  # type: ignore[arg-type]


def test_conversation_refuses_a_naive_timestamp() -> None:
    with pytest.raises(DomainError):
        Conversation(ConversationId(uuid4()), new_tenant(), new_principal(), datetime(2026, 1, 1))


# ---- Message ----------------------------------------------------------------------------------
def user_message(**over: object) -> Message:
    args: dict[str, object] = {
        "tenant_id": new_tenant(),
        "conversation_id": ConversationId(uuid4()),
        "sequence": 1,
        "client_message_id": ClientMessageId(uuid4()),
        "text": MessageText("hello"),
        "now": NOW,
        **over,
    }
    return Message.user(**args)  # type: ignore[arg-type]


def assistant_message(**over: object) -> Message:
    args: dict[str, object] = {
        "tenant_id": new_tenant(),
        "conversation_id": ConversationId(uuid4()),
        "sequence": 2,
        "generation_record_id": uuid4(),
        "now": NOW,
        **over,
    }
    return Message.assistant(**args)  # type: ignore[arg-type]


def test_a_user_message_holds_text_and_a_client_id_only() -> None:
    m = user_message()
    assert m.role is MessageRole.USER and m.text is not None and m.generation_record_id is None


def test_an_assistant_message_holds_a_record_id_and_never_text() -> None:
    m = assistant_message()
    assert m.role is MessageRole.ASSISTANT and m.text is None and m.client_message_id is None
    assert m.generation_record_id is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"role": MessageRole.USER},  # no client id, no text
        {"role": MessageRole.USER, "client_message_id": ClientMessageId(uuid4())},  # no text
        {"role": MessageRole.USER, "text": MessageText("x")},  # no client id
        {  # a user message that also claims a record
            "role": MessageRole.USER,
            "client_message_id": ClientMessageId(uuid4()),
            "text": MessageText("x"),
            "generation_record_id": uuid4(),
        },
        {"role": MessageRole.ASSISTANT},  # no record
        {  # an assistant message can never carry answer text
            "role": MessageRole.ASSISTANT,
            "generation_record_id": uuid4(),
            "text": MessageText("an answer"),
        },
        {  # ... nor a client id
            "role": MessageRole.ASSISTANT,
            "generation_record_id": uuid4(),
            "client_message_id": ClientMessageId(uuid4()),
        },
    ],
)
def test_no_other_message_shape_can_be_built(fields: dict[str, object]) -> None:
    base = {
        "tenant_id": new_tenant(),
        "conversation_id": ConversationId(uuid4()),
        "sequence": 1,
        "created_at": NOW,
    }
    with pytest.raises(DomainError):
        Message(**{**base, **fields})  # type: ignore[arg-type]


@pytest.mark.parametrize("sequence", [0, -1, HARD_MAX_MESSAGES + 1, True])
def test_message_sequence_is_bounded(sequence: object) -> None:
    with pytest.raises(DomainError):
        user_message(sequence=sequence)


def test_messages_are_immutable_and_hide_their_text() -> None:
    m = user_message(text=MessageText(PRIVATE_TEXT))
    with pytest.raises(AttributeError):
        m.sequence = 9  # type: ignore[misc]
    assert PRIVATE_TEXT not in repr(m)


# ---- DeletionReceipt --------------------------------------------------------------------------
def test_a_receipt_claims_only_its_scope_and_carries_no_principal_or_content() -> None:
    r = DeletionReceipt(new_tenant(), ConversationId(uuid4()), NOW, 3)
    assert r.scope == DELETION_SCOPE == "conversations.messages_and_rows"
    assert set(r.__dataclass_fields__) == {
        "tenant_id",
        "conversation_id",
        "deleted_at",
        "messages_deleted",
        "scope",
    }


@pytest.mark.parametrize(
    "bad", [{"messages_deleted": -1}, {"messages_deleted": 1001}, {"scope": "all"}]
)
def test_a_receipt_refuses_impossible_values(bad: dict[str, object]) -> None:
    args: dict[str, object] = {
        "tenant_id": new_tenant(),
        "conversation_id": ConversationId(uuid4()),
        "deleted_at": NOW,
        "messages_deleted": 0,
        **bad,
    }
    with pytest.raises(DomainError):
        DeletionReceipt(**args)  # type: ignore[arg-type]


# ---- limits and settings ----------------------------------------------------------------------
def test_limits_are_bounded_by_the_hard_ceilings() -> None:
    ConversationLimits(HARD_MAX_MESSAGES, HARD_MAX_TEXT_CHARS, HARD_MAX_LIST)
    for args in (
        (0, 10, 10),
        (HARD_MAX_MESSAGES + 1, 10, 10),
        (10, 0, 10),
        (10, HARD_MAX_TEXT_CHARS + 1, 10),
        (10, 10, 0),
        (10, 10, HARD_MAX_LIST + 1),
    ):
        with pytest.raises(ValidationError) as err:
            ConversationLimits(*args)
        assert err.value.code == codes.LIMITS_INVALID


def test_settings_defaults_are_safe_and_the_bounds_equal_the_ceilings() -> None:
    s = ConversationsSettings()
    assert (s.max_messages, s.max_text_chars, s.max_list) == (200, 8_000, 50)
    ConversationsSettings(max_messages=1000, max_text_chars=32_000, max_list=200)
    for bad in ({"max_messages": 1001}, {"max_text_chars": 32_001}, {"max_list": 201}, {"x": 1}):
        with pytest.raises(SettingsError):
            ConversationsSettings(**bad)


def test_the_database_ceilings_equal_the_domain_ceilings() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert re.search(rf"message_count BETWEEN 0 AND {HARD_MAX_MESSAGES}\b", sql)
    assert re.search(rf"sequence BETWEEN 1 AND {HARD_MAX_MESSAGES}\b", sql)
    assert re.search(rf"messages_deleted BETWEEN 0 AND {HARD_MAX_MESSAGES}\b", sql)
    assert f"char_length(text) <= {HARD_MAX_TEXT_CHARS}" in sql
    assert f"scope = '{DELETION_SCOPE}'" in sql or "_DELETE_SCOPE" in sql
