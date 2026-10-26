"""Conversations through the real composition root against real Postgres (ADR-039).

Real adapters end to end: the production `citations` PostgreSQL record repository, the real
`SqlAuditRecorder` (its CHECKs validate the audit attributes), forced RLS and the conversations
migration branch. No fake is used: the platform's real clock and id generator are wired.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.bootstrap.audit import AUDIT_MIGRATIONS, build_audit
from lda.bootstrap.citations import CITATIONS_MIGRATIONS, build_generation_records
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.conversations import (
    CONVERSATIONS_MIGRATIONS,
    ConversationsContainer,
    build_conversations,
    load_conversations_settings,
)
from lda.modules.conversations.api import (
    NOT_FOUND,
    RECORD_LINKED,
    RECORD_UNAVAILABLE,
    AppendAssistantTurnCommand,
    AppendUserMessageCommand,
    ClientMessageId,
    ConversationReference,
    StartConversationCommand,
)
from lda.modules.conversations.tests.support import (
    PRIVATE_TEXT,
    a_record,
    ctx_for,
    new_principal,
    new_tenant,
)
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.database import migrate
from lda.shared.kernel.errors import ConflictError, NotFoundError
from platform_support import Database, provisioned_database

ROLE = "lda_app_conversations_boot"
MIGRATIONS = [AUDIT_MIGRATIONS, CITATIONS_MIGRATIONS, CONVERSATIONS_MIGRATIONS]


def environment(db: Database) -> dict[str, str]:
    return {
        "LDA_DATABASE__URL": db.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": "bucket",
        "LDA_JOBS__QUEUES": '{"events": 1}',
        "LDA_CONVERSATIONS__MAX_MESSAGES": "3",
    }


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_conversations_boot", ROLE, MIGRATIONS) as db:
        yield db


@pytest.fixture
def stack(database: Database) -> Iterator[tuple[ConversationsContainer, PlatformContainer]]:
    config = ConfigurationLoader(environment(database))
    platform = build_platform(config, audit_factory=build_audit().recorder_factory)
    try:
        container = build_conversations(
            platform, load_conversations_settings(config), build_generation_records(platform)
        )
        yield container, platform
    finally:
        platform.dispose()


def test_a_conversation_runs_end_to_end_on_the_real_adapters(
    stack: tuple[ConversationsContainer, PlatformContainer], database: Database
) -> None:
    conversations, platform = stack
    records = build_generation_records(platform)
    tenant, owner = new_tenant(), new_principal()
    ctx = ctx_for(tenant, owner)
    record = a_record(tenant, owner)
    records.add(record)

    cid = conversations.start_conversation.execute(ctx, StartConversationCommand()).conversation_id
    conversations.append_user_message.execute(
        ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), PRIVATE_TEXT)
    )
    turn = conversations.append_assistant_turn.execute(
        ctx, AppendAssistantTurnCommand(cid, record.record_id)
    )
    assert turn.message.generation_record_id == record.record_id and turn.message.text is None
    history = conversations.read_conversation.execute(ctx, ConversationReference(cid))
    assert [m.sequence for m in history.messages] == [1, 2]

    # the real audit table accepted every fact, and none of them holds the user's text
    admin: Engine = create_engine(database.admin_url)
    try:
        with admin.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT action, resource_id, actor::text, attributes::text "
                    "FROM audit.records WHERE tenant_id = :t ORDER BY record_id"
                ),
                {"t": tenant.value},
            ).all()
        assert [r[0] for r in rows] == [
            "conversations.conversation_started",
            "conversations.user_message_added",
            "conversations.assistant_turn_added",
        ]
        assert all(r[1] == str(cid) and r[2] == str(owner) for r in rows)
        assert PRIVATE_TEXT not in " ".join(str(c) for r in rows for c in r)
        assert str(record.record_id) in rows[2][3]
    finally:
        admin.dispose()


def test_the_real_record_repository_enforces_tenant_and_principal(
    stack: tuple[ConversationsContainer, PlatformContainer],
) -> None:
    conversations, platform = stack
    records = build_generation_records(platform)
    tenant, owner = new_tenant(), new_principal()
    ctx = ctx_for(tenant, owner)
    cid = conversations.start_conversation.execute(ctx, StartConversationCommand()).conversation_id
    someone_else = a_record(tenant, new_principal())
    other_tenant = a_record(new_tenant(), owner)
    for record in (someone_else, other_tenant):
        records.add(record)
    for record_id in (someone_else.record_id, other_tenant.record_id, uuid4()):
        with pytest.raises(NotFoundError) as err:
            conversations.append_assistant_turn.execute(
                ctx, AppendAssistantTurnCommand(cid, record_id)
            )
        assert err.value.code == RECORD_UNAVAILABLE
    assert (
        conversations.read_conversation.execute(
            ctx, ConversationReference(cid)
        ).conversation.message_count
        == 0
    )


def test_deleting_a_conversation_leaves_the_generation_record_and_audit_history_alone(
    stack: tuple[ConversationsContainer, PlatformContainer], database: Database
) -> None:
    conversations, platform = stack
    records = build_generation_records(platform)
    tenant, owner = new_tenant(), new_principal()
    ctx = ctx_for(tenant, owner)
    record = a_record(tenant, owner)
    records.add(record)
    cid = conversations.start_conversation.execute(ctx, StartConversationCommand()).conversation_id
    conversations.append_assistant_turn.execute(
        ctx, AppendAssistantTurnCommand(cid, record.record_id)
    )
    conversations.delete_conversation.execute(ctx, ConversationReference(cid))
    assert records.get(tenant, record.record_id) == record  # owned by citations: untouched
    admin: Engine = create_engine(database.admin_url)
    try:
        with admin.connect() as conn:
            actions = [
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT action FROM audit.records WHERE tenant_id = :t ORDER BY record_id"
                    ),
                    {"t": tenant.value},
                )
            ]
            receipt = conn.execute(
                text(
                    "SELECT messages_deleted, scope FROM conversations.deletion_receipts "
                    "WHERE tenant_id = :t AND conversation_id = :c"
                ),
                {"t": tenant.value, "c": cid.value},
            ).one()
    finally:
        admin.dispose()
    assert actions == [  # earlier audit history is intact, and the deletion was added to it
        "conversations.conversation_started",
        "conversations.assistant_turn_added",
        "conversations.conversation_deleted",
    ]
    assert tuple(receipt) == (1, "conversations.messages_and_rows")
    with pytest.raises(NotFoundError) as err:
        conversations.read_conversation.execute(ctx, ConversationReference(cid))
    assert err.value.code == NOT_FOUND
    # the record is free to be linked again (no trace of the link remains)
    other = conversations.start_conversation.execute(ctx, StartConversationCommand())
    again = conversations.append_assistant_turn.execute(
        ctx, AppendAssistantTurnCommand(other.conversation_id, record.record_id)
    )
    assert not again.replayed


def test_a_record_linked_in_one_conversation_conflicts_in_another(
    stack: tuple[ConversationsContainer, PlatformContainer],
) -> None:
    conversations, platform = stack
    records = build_generation_records(platform)
    tenant, owner = new_tenant(), new_principal()
    ctx = ctx_for(tenant, owner)
    record = a_record(tenant, owner)
    records.add(record)
    first = conversations.start_conversation.execute(ctx, StartConversationCommand())
    second = conversations.start_conversation.execute(ctx, StartConversationCommand())
    conversations.append_assistant_turn.execute(
        ctx, AppendAssistantTurnCommand(first.conversation_id, record.record_id)
    )
    with pytest.raises(ConflictError) as err:
        conversations.append_assistant_turn.execute(
            ctx, AppendAssistantTurnCommand(second.conversation_id, record.record_id)
        )
    assert err.value.code == RECORD_LINKED


def test_configuration_reaches_the_use_cases(
    stack: tuple[ConversationsContainer, PlatformContainer],
) -> None:
    conversations, _ = stack  # LDA_CONVERSATIONS__MAX_MESSAGES=3
    ctx = ctx_for(new_tenant(), new_principal())
    cid = conversations.start_conversation.execute(ctx, StartConversationCommand()).conversation_id
    for n in range(3):
        conversations.append_user_message.execute(
            ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), f"m{n}")
        )
    with pytest.raises(ConflictError) as err:
        conversations.append_user_message.execute(
            ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "four")
        )
    assert err.value.code == "conversation_limit_reached"


def test_the_conversations_branch_downgrades_and_upgrades_cleanly(database: Database) -> None:
    migrate.downgrade(
        database.admin_url,
        target="conversations@base",
        app_role=ROLE,
        extra_locations=MIGRATIONS,
    )
    admin: Engine = create_engine(database.admin_url)
    try:
        with admin.connect() as conn:
            gone = conn.execute(text("SELECT to_regclass('conversations.messages')")).scalar_one()
            kept = conn.execute(
                text("SELECT to_regclass('citations.generation_records')")
            ).scalar_one()
        assert gone is None and kept is not None  # only this branch was removed
    finally:
        admin.dispose()
    migrate.upgrade(database.admin_url, app_role=ROLE, extra_locations=MIGRATIONS)
