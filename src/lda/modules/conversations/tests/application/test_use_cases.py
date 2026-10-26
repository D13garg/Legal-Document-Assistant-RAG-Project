"""Behaviour of every use case. Each test runs on the in-memory twin AND on real PostgreSQL (the
`kit` fixture), so the two cannot drift."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from functools import partial
from uuid import UUID, uuid4

import pytest

from lda.modules.citations.api import GenerationRecord
from lda.modules.conversations.api import (
    LIMIT_REACHED,
    MESSAGE_CONFLICT,
    MESSAGE_INVALID,
    NOT_FOUND,
    RECORD_LINKED,
    RECORD_UNAVAILABLE,
    AppendAssistantTurn,
    AppendAssistantTurnCommand,
    AppendedMessageView,
    AppendUserMessageCommand,
    ClientMessageId,
    ConversationId,
    ConversationReference,
    ListConversationsQuery,
    MessageRole,
    StartConversationCommand,
)
from lda.modules.conversations.tests.support import (
    LIMITS,
    PRIVATE_TEXT,
    Kit,
    a_record,
    ctx_for,
    new_principal,
    new_tenant,
)
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import (
    AuthorizationError,
    ConflictError,
    LdaError,
    NotFoundError,
    ValidationError,
)
from lda.shared.kernel.ids import TenantId


def started(kit: Kit) -> ConversationId:
    return kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id


def say(
    kit: Kit, cid: ConversationId, text: str = "hello", client: ClientMessageId | None = None
) -> AppendedMessageView:
    return kit.append_user.execute(
        kit.ctx, AppendUserMessageCommand(cid, client or ClientMessageId(uuid4()), text)
    )


def link(kit: Kit, cid: ConversationId) -> tuple[GenerationRecord, AppendedMessageView]:
    record = a_record(kit.tenant, kit.owner)
    kit.records.add(record)
    return record, kit.append_assistant.execute(
        kit.ctx, AppendAssistantTurnCommand(cid, record.record_id)
    )


# ---- start / read -----------------------------------------------------------------------------
def test_a_started_conversation_is_empty_and_readable_by_its_owner(kit: Kit) -> None:
    view = kit.start.execute(kit.ctx, StartConversationCommand())
    history = kit.read.execute(kit.ctx, ConversationReference(view.conversation_id))
    assert history.conversation == view and view.message_count == 0 and history.messages == ()


def test_every_start_creates_a_new_conversation(kit: Kit) -> None:
    assert started(kit) != started(kit)


def test_messages_get_contiguous_sequences_and_keep_their_exact_text(kit: Kit) -> None:
    cid = started(kit)
    first = say(kit, cid, "  first line\nsecond  ")
    record, turn = link(kit, cid)
    third = say(kit, cid, "third")
    assert [m.message.sequence for m in (first, turn, third)] == [1, 2, 3]
    assert not first.replayed and not turn.replayed and not third.replayed
    messages = kit.read.execute(kit.ctx, ConversationReference(cid)).messages
    assert [m.role for m in messages] == [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.USER]
    assert messages[0].text == "  first line\nsecond  " and messages[2].text == "third"
    assert messages[1].generation_record_id == record.record_id
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 3


# ---- user message idempotency and limits ------------------------------------------------------
def test_repeating_a_client_message_id_with_the_same_text_returns_the_original(kit: Kit) -> None:
    cid, client = started(kit), ClientMessageId(uuid4())
    first = say(kit, cid, "same", client)
    kit.clock.advance(timedelta(hours=1))
    again = say(kit, cid, "same", client)
    assert again.replayed and not first.replayed
    assert again.message == first.message  # same sequence, same original timestamp
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 1


def test_reusing_a_client_message_id_with_other_text_is_a_content_free_conflict(kit: Kit) -> None:
    cid, client = started(kit), ClientMessageId(uuid4())
    say(kit, cid, f"original {PRIVATE_TEXT}", client)
    with pytest.raises(ConflictError) as err:
        say(kit, cid, "different text", client)
    assert err.value.code == MESSAGE_CONFLICT
    assert PRIVATE_TEXT not in repr(err.value) + str(err.value) + err.value.detail
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 1


def test_the_same_client_message_id_in_another_conversation_is_independent(kit: Kit) -> None:
    a, b, client = started(kit), started(kit), ClientMessageId(uuid4())
    assert not say(kit, a, "x", client).replayed
    assert not say(kit, b, "y", client).replayed


def test_a_full_conversation_refuses_new_messages_but_still_replays_old_ones(kit: Kit) -> None:
    cid = started(kit)
    clients = [ClientMessageId(uuid4()) for _ in range(4)]
    for n, client in enumerate(clients):
        say(kit, cid, f"m{n}", client)
    with pytest.raises(ConflictError) as err:
        say(kit, cid, "one too many")
    assert err.value.code == LIMIT_REACHED
    with pytest.raises(ConflictError) as err2:
        link(kit, cid)
    assert err2.value.code == LIMIT_REACHED
    assert say(kit, cid, "m0", clients[0]).replayed  # an old message is still returned
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 4


@pytest.mark.parametrize("bad", ["", "  \n", "a\x00b", "x" * 61])
def test_invalid_text_is_refused_and_nothing_is_stored(kit: Kit, bad: str) -> None:
    cid = started(kit)
    with pytest.raises(ValidationError) as err:
        say(kit, cid, bad)
    assert err.value.code == MESSAGE_INVALID
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).messages == ()


def test_text_at_exactly_the_limit_is_accepted(kit: Kit) -> None:
    assert say(kit, started(kit), "x" * 60).message.text == "x" * 60


def test_text_with_emoji_and_non_latin_scripts_round_trips(kit: Kit) -> None:
    cid, text = started(kit), "Haftung §7 \u2013 契約 ⚖️ 😀"
    say(kit, cid, text)
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).messages[0].text == text


# ---- assistant turns --------------------------------------------------------------------------
def test_an_assistant_turn_stores_only_the_record_reference(kit: Kit) -> None:
    cid = started(kit)
    record, turn = link(kit, cid)
    view = turn.message
    assert view.role is MessageRole.ASSISTANT and view.generation_record_id == record.record_id
    assert view.text is None and view.text_stored is False and view.client_message_id is None


def test_linking_the_same_record_again_replays_it(kit: Kit) -> None:
    cid = started(kit)
    record, first = link(kit, cid)
    again = kit.append_assistant.execute(kit.ctx, AppendAssistantTurnCommand(cid, record.record_id))
    assert again.replayed and again.message == first.message
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 1


def test_a_record_already_linked_to_another_conversation_is_a_conflict(kit: Kit) -> None:
    a, b = started(kit), started(kit)
    record, _ = link(kit, a)
    with pytest.raises(ConflictError) as err:
        kit.append_assistant.execute(kit.ctx, AppendAssistantTurnCommand(b, record.record_id))
    assert err.value.code == RECORD_LINKED
    assert kit.read.execute(kit.ctx, ConversationReference(b)).messages == ()


def test_an_unusable_record_is_one_indistinguishable_failure(kit: Kit) -> None:
    cid = started(kit)
    other_principal = a_record(kit.tenant, new_principal())
    other_tenant = a_record(new_tenant(), kit.owner)
    for record in (other_principal, other_tenant):
        kit.records.add(record)
    failures = []
    for record_id in (uuid4(), other_principal.record_id, other_tenant.record_id):
        with pytest.raises(NotFoundError) as err:
            kit.append_assistant.execute(kit.ctx, AppendAssistantTurnCommand(cid, record_id))
        failures.append((err.value.code, str(err.value), err.value.detail))
    assert failures[0] == failures[1] == failures[2]
    assert failures[0][0] == RECORD_UNAVAILABLE
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).messages == ()


def test_the_record_failure_never_reveals_whether_the_conversation_exists(kit: Kit) -> None:
    """A bad record answers the same for a real conversation and for a missing one."""
    real, missing = started(kit), ConversationId(uuid4())
    seen = set()
    for cid in (real, missing):
        with pytest.raises(NotFoundError) as err:
            kit.append_assistant.execute(kit.ctx, AppendAssistantTurnCommand(cid, uuid4()))
        seen.add(err.value.code)
    assert seen == {RECORD_UNAVAILABLE}


# ---- ownership: owner-only, indistinguishable failures ----------------------------------------
def test_missing_foreign_tenant_and_not_owned_conversations_fail_identically(kit: Kit) -> None:
    cid = started(kit)
    say(kit, cid, "private")
    record = a_record(kit.tenant, new_principal())
    kit.records.add(record)
    intruders = {
        "other principal": kit.other_principal(),
        "other tenant": kit.other_tenant(),
    }
    for name, who in intruders.items():
        mine = a_record(who.tenant_id, who.principal_id)
        kit.records.add(mine)
        calls: dict[str, Callable[[], object]] = {
            "read": partial(kit.read.execute, who, ConversationReference(cid)),
            "append": partial(
                kit.append_user.execute,
                who,
                AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "hi"),
            ),
            "assistant": partial(
                kit.append_assistant.execute,
                who,
                AppendAssistantTurnCommand(cid, mine.record_id),
            ),
            "delete": partial(kit.delete.execute, who, ConversationReference(cid)),
        }
        for call, run in calls.items():
            with pytest.raises(NotFoundError) as err:
                run()
            assert (err.value.code, str(err.value), err.value.detail) == (
                NOT_FOUND,
                NOT_FOUND,
                "",
            ), (
                name,
                call,
            )
    missing = ConversationReference(ConversationId(uuid4()))
    with pytest.raises(NotFoundError) as err:
        kit.read.execute(kit.ctx, missing)
    assert (err.value.code, str(err.value), err.value.detail) == (NOT_FOUND, NOT_FOUND, "")
    # the owner's conversation is untouched by every attempt
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 1


def test_system_context_has_no_principal_and_is_refused_everywhere(kit: Kit) -> None:
    system = SystemContext(kit.tenant, "maintenance")
    cid = started(kit)
    calls = (
        lambda: kit.start.execute(system, StartConversationCommand()),
        lambda: kit.append_user.execute(
            system, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "x")
        ),
        lambda: kit.append_assistant.execute(system, AppendAssistantTurnCommand(cid, uuid4())),
        lambda: kit.read.execute(system, ConversationReference(cid)),
        lambda: kit.listing.execute(system, ListConversationsQuery(limit=3)),
        lambda: kit.delete.execute(system, ConversationReference(cid)),
    )
    for call in calls:
        with pytest.raises(AuthorizationError) as err:
            call()
        assert err.value.code == "conversation_requires_principal"


# ---- listing ----------------------------------------------------------------------------------
def test_listing_returns_only_the_callers_conversations_newest_first(kit: Kit) -> None:
    ids = []
    for _ in range(3):
        ids.append(started(kit))
        kit.clock.advance(timedelta(minutes=1))
    mine_elsewhere = kit.other_principal()
    kit.start.execute(mine_elsewhere, StartConversationCommand())
    kit.start.execute(kit.other_tenant(), StartConversationCommand())
    listed = kit.listing.execute(kit.ctx, ListConversationsQuery(limit=3)).conversations
    assert [c.conversation_id for c in listed] == list(reversed(ids))
    assert len(kit.listing.execute(kit.ctx, ListConversationsQuery(limit=2)).conversations) == 2
    assert (
        len(kit.listing.execute(mine_elsewhere, ListConversationsQuery(limit=3)).conversations) == 1
    )


@pytest.mark.parametrize("limit", [0, -1, 4, True])
def test_listing_refuses_an_unbounded_or_invalid_limit(kit: Kit, limit: int) -> None:
    with pytest.raises(ValidationError):
        kit.listing.execute(kit.ctx, ListConversationsQuery(limit=limit))


# ---- deletion ---------------------------------------------------------------------------------
def test_deleting_removes_the_conversation_and_every_message(kit: Kit) -> None:
    cid, other = started(kit), started(kit)
    say(kit, cid, "one")
    record, _ = link(kit, cid)
    say(kit, cid, "three")
    say(kit, other, "keep me")
    view = kit.delete.execute(kit.ctx, ConversationReference(cid))
    assert (view.conversation_id, view.messages_deleted) == (cid, 3)
    assert view.scope == "conversations.messages_and_rows"
    with pytest.raises(NotFoundError):
        kit.read.execute(kit.ctx, ConversationReference(cid))
    assert kit.read.execute(kit.ctx, ConversationReference(other)).messages[0].text == "keep me"
    # the generation record is owned by citations and is not touched
    assert kit.records.get(kit.tenant, record.record_id) == record
    # a deleted conversation's record can be linked again: nothing of the link remains
    again = kit.append_assistant.execute(
        kit.ctx, AppendAssistantTurnCommand(other, record.record_id)
    )
    assert not again.replayed


def test_deleting_twice_fails_like_any_absent_conversation(kit: Kit) -> None:
    cid = started(kit)
    kit.delete.execute(kit.ctx, ConversationReference(cid))
    with pytest.raises(NotFoundError) as err:
        kit.delete.execute(kit.ctx, ConversationReference(cid))
    assert err.value.code == NOT_FOUND


def test_an_empty_conversation_can_be_deleted(kit: Kit) -> None:
    view = kit.delete.execute(kit.ctx, ConversationReference(started(kit)))
    assert view.messages_deleted == 0


# ---- nothing leaks ----------------------------------------------------------------------------
def test_text_never_reaches_a_repr_or_an_error(kit: Kit) -> None:
    cid = started(kit)
    command = AppendUserMessageCommand(cid, ClientMessageId(uuid4()), PRIVATE_TEXT)
    result = kit.append_user.execute(kit.ctx, command)
    history = kit.read.execute(kit.ctx, ConversationReference(cid))
    for obj in (command, result, result.message, history, history.messages[0]):
        assert PRIVATE_TEXT not in repr(obj)
    assert history.messages[0].text == PRIVATE_TEXT  # the owner does get their own text back
    with pytest.raises(LdaError) as err:
        kit.append_user.execute(
            kit.other_principal(),
            AppendUserMessageCommand(cid, ClientMessageId(uuid4()), PRIVATE_TEXT),
        )
    assert PRIVATE_TEXT not in repr(err.value) + err.value.detail + str(err.value.__cause__)


def test_context_from_another_tenant_cannot_see_a_conversation_it_could_name(kit: Kit) -> None:
    cid = started(kit)
    twin = ctx_for(new_tenant(), kit.owner)  # same principal id, different tenant
    with pytest.raises(NotFoundError):
        kit.read.execute(twin, ConversationReference(cid))


class LeakyRecords:
    """A misbehaving record repository that ignores the tenant it is asked about. The use case
    must not rely on it: it re-checks tenant and principal on whatever comes back."""

    def __init__(self, record: GenerationRecord) -> None:
        self._record = record

    def add(self, record: GenerationRecord) -> None:  # pragma: no cover
        raise AssertionError("read-only double")

    def get(self, tenant_id: TenantId, record_id: UUID) -> GenerationRecord | None:
        return self._record


def test_a_misbehaving_record_repository_cannot_link_a_foreign_record(kit: Kit) -> None:
    cid = started(kit)
    for foreign in (
        a_record(new_tenant(), kit.owner),  # right principal id, wrong tenant
        a_record(kit.tenant, new_principal()),  # right tenant, wrong principal
    ):
        leaky = AppendAssistantTurn(kit.factory, LeakyRecords(foreign), kit.clock, LIMITS)
        with pytest.raises(NotFoundError) as err:
            leaky.execute(kit.ctx, AppendAssistantTurnCommand(cid, foreign.record_id))
        assert err.value.code == RECORD_UNAVAILABLE
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).messages == ()
