"""Real PostgreSQL: privileges, immutability triggers, CHECKs, uniqueness, forced RLS, commit-time
consistency, atomic rollback, error hygiene and concurrent races. Enforcement is the database's,
not the application's: most tests bypass the use cases on purpose."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from lda.modules.conversations.api import (
    MESSAGE_CONFLICT,
    NOT_FOUND,
    RECORD_LINKED,
    AppendAssistantTurn,
    AppendAssistantTurnCommand,
    AppendUserMessage,
    AppendUserMessageCommand,
    ClientMessageId,
    ConversationId,
    ConversationLimits,
    ConversationReference,
    DeleteConversation,
    ListConversations,
    ReadConversation,
    StartConversation,
    StartConversationCommand,
)
from lda.modules.conversations.domain import codes
from lda.modules.conversations.domain.conversation import Conversation
from lda.modules.conversations.domain.message import Message
from lda.modules.conversations.domain.text import MessageText
from lda.modules.conversations.infrastructure.persistence.unit_of_work import (
    SqlConversationsUnitOfWorkFactory,
)
from lda.modules.conversations.tests.support import (
    LIMITS,
    PRIVATE_TEXT,
    START,
    Kit,
    a_record,
    build_kit,
    new_principal,
    new_tenant,
)
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConflictError, InfrastructureError, NotFoundError
from lda.shared.kernel.ids import PrincipalId, TenantId
from lda.shared.ports.audit import AuditFact
from platform_support import make_wiring

CONV = (
    "INSERT INTO conversations.conversations (tenant_id, id, owner_id, created_at, message_count)"
)
MSG = (
    "INSERT INTO conversations.messages (tenant_id, conversation_id, sequence, role, created_at, "
    "client_message_id, text, record_id)"
)


RECEIPT_INSERT = (
    "INSERT INTO conversations.deletion_receipts "
    "VALUES (:t, :c, now(), 0, 'conversations.messages_and_rows')"
)


def seed(admin: Engine, tenant: TenantId, owner: PrincipalId, users: int = 1) -> UUID:
    """A consistent conversation with `users` user messages, inserted as the superuser."""
    cid = uuid4()
    with admin.begin() as conn:
        conn.execute(
            text(f"{CONV} VALUES (:t, :c, :o, now(), :n)"),
            {"t": tenant.value, "c": cid, "o": owner.value, "n": users},
        )
        for n in range(1, users + 1):
            conn.execute(
                text(f"{MSG} VALUES (:t, :c, :n, 'user', now(), :m, 'hi', NULL)"),
                {"t": tenant.value, "c": cid, "n": n, "m": uuid4()},
            )
    return cid


def constraint(exc: BaseException) -> str | None:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None)


def sqlstate(exc: BaseException) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def run_admin(admin: Engine, sql: str, **params: object) -> None:
    with admin.begin() as conn:
        conn.execute(text(sql), params)


def count(admin: Engine, sql: str, **params: object) -> int:
    with admin.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


# ---- structure --------------------------------------------------------------------------------
def test_tables_live_only_in_the_conversations_schema_with_no_foreign_key_out(
    admin_engine: Engine,
) -> None:
    with admin_engine.connect() as conn:
        where = conn.execute(
            text(
                "SELECT DISTINCT n.nspname FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace WHERE c.relname IN "
                "('conversations', 'messages', 'deletion_receipts') AND c.relkind = 'r'"
            )
        ).all()
        foreign = conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_class r ON r.oid = c.confrelid WHERE c.contype = 'f' "
                "AND t.relnamespace = 'conversations'::regnamespace "
                "AND r.relnamespace <> 'conversations'::regnamespace"
            )
        ).scalar_one()
    assert [r[0] for r in where] == ["conversations"] and foreign == 0


def test_every_table_has_forced_row_level_security(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relnamespace = 'conversations'::regnamespace AND relkind = 'r'"
            )
        ).all()
    assert {r[0] for r in rows} == {"conversations", "messages", "deletion_receipts"}
    assert all(r[1] and r[2] for r in rows)


# ---- the runtime role's privileges ------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE conversations.messages SET text = 'x'",
        "DELETE FROM conversations.messages",
        "TRUNCATE conversations.messages",
        "UPDATE conversations.conversations SET owner_id = gen_random_uuid()",
        "UPDATE conversations.conversations SET created_at = now()",
        "TRUNCATE conversations.conversations",
        "UPDATE conversations.deletion_receipts SET messages_deleted = 0",
        "DELETE FROM conversations.deletion_receipts",
        "TRUNCATE conversations.deletion_receipts",
    ],
)
def test_the_runtime_role_cannot_change_what_must_not_change(app_engine: Engine, sql: str) -> None:
    with pytest.raises(DBAPIError) as err, TenantTransaction(app_engine, new_tenant()) as tx:
        tx.session.execute(text(sql))
    assert sqlstate(err.value) == "42501"  # insufficient_privilege


# ---- immutability holds even for the owner/superuser (triggers) -------------------------------
def test_a_message_can_never_be_updated(admin_engine: Engine) -> None:
    tenant, owner = new_tenant(), new_principal()
    seed(admin_engine, tenant, owner)
    with pytest.raises(DBAPIError) as err:
        run_admin(
            admin_engine,
            "UPDATE conversations.messages SET text = 'edited' WHERE tenant_id = :t",
            t=tenant.value,
        )
    assert sqlstate(err.value) == "55000"


def test_a_message_cannot_be_deleted_while_its_conversation_exists(admin_engine: Engine) -> None:
    tenant, owner = new_tenant(), new_principal()
    seed(admin_engine, tenant, owner, users=2)
    with pytest.raises(DBAPIError) as err:
        run_admin(
            admin_engine, "DELETE FROM conversations.messages WHERE tenant_id = :t", t=tenant.value
        )
    assert sqlstate(err.value) == "55000"
    assert (
        count(
            admin_engine,
            "SELECT count(*) FROM conversations.messages WHERE tenant_id = :t",
            t=tenant.value,
        )
        == 2
    )


def test_deleting_the_conversation_is_the_only_way_messages_disappear(admin_engine: Engine) -> None:
    tenant, owner = new_tenant(), new_principal()
    cid = seed(admin_engine, tenant, owner, users=3)
    run_admin(admin_engine, "DELETE FROM conversations.conversations WHERE id = :c", c=cid)
    assert (
        count(
            admin_engine,
            "SELECT count(*) FROM conversations.messages WHERE conversation_id = :c",
            c=cid,
        )
        == 0
    )


@pytest.mark.parametrize(
    "sql",
    [
        "TRUNCATE conversations.messages CASCADE",
        "TRUNCATE conversations.conversations CASCADE",
        "TRUNCATE conversations.deletion_receipts CASCADE",
    ],
)
def test_no_role_can_truncate_a_table(admin_engine: Engine, sql: str) -> None:
    with pytest.raises(DBAPIError) as err:
        run_admin(admin_engine, sql)
    assert sqlstate(err.value) == "55000"


def test_a_conversation_can_only_grow_by_one_and_keeps_its_owner(admin_engine: Engine) -> None:
    tenant, owner = new_tenant(), new_principal()
    cid = seed(admin_engine, tenant, owner)
    for sql in (
        "UPDATE conversations.conversations SET owner_id = gen_random_uuid() WHERE id = :c",
        "UPDATE conversations.conversations "
        "SET created_at = now() + interval '1 day' WHERE id = :c",
        "UPDATE conversations.conversations SET message_count = message_count + 2 WHERE id = :c",
        "UPDATE conversations.conversations SET message_count = message_count - 1 WHERE id = :c",
        "UPDATE conversations.conversations SET message_count = message_count WHERE id = :c",
    ):
        with pytest.raises(DBAPIError) as err:
            run_admin(admin_engine, sql, c=cid)
        assert sqlstate(err.value) == "55000", sql


def test_receipts_are_insert_only(admin_engine: Engine) -> None:
    tenant, cid = new_tenant(), uuid4()
    run_admin(
        admin_engine,
        RECEIPT_INSERT,
        t=tenant.value,
        c=cid,
    )
    for sql in (
        "UPDATE conversations.deletion_receipts SET messages_deleted = 1 "
        "WHERE conversation_id = :c",
        "DELETE FROM conversations.deletion_receipts WHERE conversation_id = :c",
    ):
        with pytest.raises(DBAPIError) as err:
            run_admin(admin_engine, sql, c=cid)
        assert sqlstate(err.value) == "55000"


# ---- commit-time consistency of the counter ---------------------------------------------------
def test_a_message_without_its_counter_step_cannot_commit(admin_engine: Engine) -> None:
    tenant, owner = new_tenant(), new_principal()
    cid = seed(admin_engine, tenant, owner)
    with pytest.raises(DBAPIError) as err:
        run_admin(
            admin_engine,
            f"{MSG} VALUES (:t, :c, 2, 'user', now(), gen_random_uuid(), 'x', NULL)",
            t=tenant.value,
            c=cid,
        )
    assert sqlstate(err.value) == "23514"


def test_a_counter_step_without_a_message_cannot_commit(admin_engine: Engine) -> None:
    tenant, owner = new_tenant(), new_principal()
    cid = seed(admin_engine, tenant, owner)
    with pytest.raises(DBAPIError) as err:
        run_admin(
            admin_engine,
            "UPDATE conversations.conversations SET message_count = 2 WHERE id = :c",
            c=cid,
        )
    assert sqlstate(err.value) == "23514"


def test_a_gap_in_the_sequence_cannot_commit(admin_engine: Engine) -> None:
    tenant, owner = new_tenant(), new_principal()
    cid = seed(admin_engine, tenant, owner)
    with pytest.raises(DBAPIError) as err, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE conversations.conversations SET message_count = 2 WHERE id = :c"),
            {"c": cid},
        )
        conn.execute(
            text(f"{MSG} VALUES (:t, :c, 3, 'user', now(), gen_random_uuid(), 'x', NULL)"),
            {"t": tenant.value, "c": cid},
        )
    assert sqlstate(err.value) == "23514"


# ---- CHECK and UNIQUE constraints -------------------------------------------------------------
SHAPES = {
    "assistant with text": ("assistant", None, "an answer", uuid4()),
    "assistant with client id": ("assistant", uuid4(), None, uuid4()),
    "assistant without record": ("assistant", None, None, None),
    "user without client id": ("user", None, "x", None),
    "user without text": ("user", uuid4(), None, None),
    "user with blank text": ("user", uuid4(), "   ", None),
    "user with a record": ("user", uuid4(), "x", uuid4()),
    "unknown role": ("system", uuid4(), "x", None),
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_message_shape_is_enforced_by_the_database(admin_engine: Engine, shape: str) -> None:
    role, client, body, record = SHAPES[shape]
    tenant, owner = new_tenant(), new_principal()
    cid = seed(admin_engine, tenant, owner, users=0)
    with pytest.raises(DBAPIError) as err, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE conversations.conversations SET message_count = 1 WHERE id = :c"),
            {"c": cid},
        )
        conn.execute(
            text(MSG + " VALUES (:t, :c, 1, :role, now(), :client, :body, :record)"),
            {
                "t": tenant.value,
                "c": cid,
                "role": role,
                "client": client,
                "body": body,
                "record": record,
            },
        )
    assert sqlstate(err.value) == "23514"


def test_oversized_text_and_counters_are_refused(admin_engine: Engine) -> None:
    tenant, owner = new_tenant(), new_principal()
    cid = seed(admin_engine, tenant, owner, users=0)
    with pytest.raises(DBAPIError) as err, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE conversations.conversations SET message_count = 1 WHERE id = :c"),
            {"c": cid},
        )
        conn.execute(
            text(
                MSG
                + " VALUES (:t, :c, 1, 'user', now(), gen_random_uuid(), repeat('x', 32001), NULL)"
            ),
            {"t": tenant.value, "c": cid},
        )
    assert sqlstate(err.value) == "23514"
    with pytest.raises(DBAPIError) as err2:
        run_admin(
            admin_engine,
            f"{CONV} VALUES (:t, gen_random_uuid(), :o, now(), 1001)",
            t=tenant.value,
            o=owner.value,
        )
    assert sqlstate(err2.value) == "23514"


def test_a_receipt_can_only_claim_its_scope(admin_engine: Engine) -> None:
    with pytest.raises(DBAPIError) as err:
        run_admin(
            admin_engine,
            "INSERT INTO conversations.deletion_receipts VALUES (:t, :c, now(), 0, 'everything')",
            t=uuid4(),
            c=uuid4(),
        )
    assert sqlstate(err.value) == "23514"


def test_uniqueness_is_a_database_constraint(admin_engine: Engine) -> None:
    tenant, owner = new_tenant(), new_principal()
    record = uuid4()
    a = seed(admin_engine, tenant, owner, users=0)
    b = seed(admin_engine, tenant, owner, users=0)
    client = uuid4()
    with admin_engine.begin() as conn:
        for cid in (a, b):
            conn.execute(
                text("UPDATE conversations.conversations SET message_count = 1 WHERE id = :c"),
                {"c": cid},
            )
        conn.execute(
            text(f"{MSG} VALUES (:t, :a, 1, 'assistant', now(), NULL, NULL, :r)"),
            {"t": tenant.value, "a": a, "r": record},
        )
        conn.execute(
            text(f"{MSG} VALUES (:t, :b, 1, 'user', now(), :m, 'x', NULL)"),
            {"t": tenant.value, "b": b, "m": client},
        )
    # the same record id in another conversation of the tenant
    with pytest.raises(DBAPIError) as err, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE conversations.conversations SET message_count = 2 WHERE id = :c"), {"c": b}
        )
        conn.execute(
            text(f"{MSG} VALUES (:t, :b, 2, 'assistant', now(), NULL, NULL, :r)"),
            {"t": tenant.value, "b": b, "r": record},
        )
    assert (sqlstate(err.value), constraint(err.value)) == ("23505", "messages_record_unique")
    # the same client id again in the same conversation
    with pytest.raises(DBAPIError) as err2, admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE conversations.conversations SET message_count = 2 WHERE id = :c"), {"c": b}
        )
        conn.execute(
            text(f"{MSG} VALUES (:t, :b, 2, 'user', now(), :m, 'y', NULL)"),
            {"t": tenant.value, "b": b, "m": client},
        )
    assert constraint(err2.value) == "messages_client_message_unique"
    # another tenant may reuse a record id: uniqueness is per tenant
    other_tenant = new_tenant()
    other = seed(admin_engine, other_tenant, owner, users=0)
    with admin_engine.begin() as conn:
        conn.execute(
            text("UPDATE conversations.conversations SET message_count = 1 WHERE id = :c"),
            {"c": other},
        )
        conn.execute(
            text(MSG + " VALUES (:t, :o, 1, 'assistant', now(), NULL, NULL, :r)"),
            {"t": other_tenant.value, "o": other, "r": record},
        )


# ---- forced row-level security ----------------------------------------------------------------
def test_another_tenant_sees_and_writes_nothing(app_engine: Engine, admin_engine: Engine) -> None:
    tenant_a, tenant_b, owner = new_tenant(), new_tenant(), new_principal()
    cid = seed(admin_engine, tenant_a, owner)
    with TenantTransaction(app_engine, tenant_b) as tx:
        for sql in (
            "SELECT count(*) FROM conversations.conversations",
            "SELECT count(*) FROM conversations.messages",
            "SELECT count(*) FROM conversations.deletion_receipts",
        ):
            assert tx.session.execute(text(sql)).scalar_one() == 0
    with pytest.raises(DBAPIError), TenantTransaction(app_engine, tenant_b) as tx:
        tx.session.execute(
            text(f"{CONV} VALUES (:t, gen_random_uuid(), :o, now(), 0)"),
            {"t": tenant_a.value, "o": owner.value},
        )
    with TenantTransaction(app_engine, tenant_a) as tx:
        assert (
            tx.session.execute(
                text("SELECT count(*) FROM conversations.messages WHERE conversation_id = :c"),
                {"c": cid},
            ).scalar_one()
            == 1
        )


def test_without_a_tenant_scope_the_runtime_role_sees_nothing(
    app_engine: Engine, admin_engine: Engine
) -> None:
    seed(admin_engine, new_tenant(), new_principal())
    with app_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM conversations.conversations")).scalar_one() == 0
        )


# ---- repository behaviour that only a database can show ---------------------------------------
def test_deleting_through_the_repository_removes_rows_but_keeps_the_receipt(
    sql_kit: Kit, admin_engine: Engine
) -> None:
    kit = sql_kit
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    kit.append_user.execute(kit.ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "a"))
    kit.delete.execute(kit.ctx, ConversationReference(cid))
    assert (
        count(
            admin_engine,
            "SELECT count(*) FROM conversations.messages "
            "WHERE tenant_id = :t AND conversation_id = :c",
            c=cid.value,
            t=kit.tenant.value,
        )
        == 0
    )
    assert (
        count(
            admin_engine,
            "SELECT messages_deleted FROM conversations.deletion_receipts "
            "WHERE tenant_id = :t AND conversation_id = :c",
            c=cid.value,
            t=kit.tenant.value,
        )
        == 1
    )


def test_a_full_size_conversation_is_deleted_in_one_transaction(
    sql_factory: SqlConversationsUnitOfWorkFactory, admin_engine: Engine
) -> None:
    kit = build_kit(sql_factory, ConversationLimits(1000, 60, 3))
    cid = uuid4()
    with admin_engine.begin() as conn:
        conn.execute(
            text(f"{CONV} VALUES (:t, :c, :o, now(), 1000)"),
            {"t": kit.tenant.value, "c": cid, "o": kit.owner.value},
        )
        conn.execute(
            text(
                "INSERT INTO conversations.messages (tenant_id, conversation_id, sequence, role, "
                "created_at, client_message_id, text, record_id) "
                "SELECT :t, :c, g, 'user', now(), gen_random_uuid(), 'x', NULL "
                "FROM generate_series(1, 1000) g"
            ),
            {"t": kit.tenant.value, "c": cid},
        )
    view = kit.delete.execute(kit.ctx, ConversationReference(ConversationId(cid)))
    assert view.messages_deleted == 1000
    assert (
        count(
            admin_engine,
            "SELECT count(*) FROM conversations.messages WHERE conversation_id = :c",
            c=cid,
        )
        == 0
    )


def test_a_failed_statement_never_carries_message_text_out(
    sql_factory: SqlConversationsUnitOfWorkFactory,
) -> None:
    """Driver errors embed statement parameters; they must be translated and not chained."""
    kit = build_kit(sql_factory)
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    client = ClientMessageId(uuid4())
    kit.append_user.execute(kit.ctx, AppendUserMessageCommand(cid, client, "first"))
    with sql_factory.begin(kit.ctx) as uow:
        conversation = uow.conversations.get_for_update(cid, kit.owner)
        assert conversation is not None
        duplicate = Message.user(
            tenant_id=kit.tenant,
            conversation_id=cid,
            sequence=conversation.next_sequence,
            client_message_id=client,
            text=MessageText(PRIVATE_TEXT),
            now=START,
        )
        with pytest.raises(ConflictError) as err:
            uow.conversations.append(conversation.advanced(), duplicate)
    assert err.value.code == MESSAGE_CONFLICT
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert PRIVATE_TEXT not in repr(err.value) + str(err.value) + err.value.detail


def test_a_commit_time_failure_is_translated_without_parameters(
    sql_factory: SqlConversationsUnitOfWorkFactory,
) -> None:
    kit = build_kit(sql_factory)
    inconsistent = Conversation(
        id=ConversationId(uuid4()),
        tenant_id=kit.tenant,
        owner=kit.owner,
        created_at=START,
        message_count=1,  # claims one message that does not exist
    )
    with pytest.raises(InfrastructureError) as err, sql_factory.begin(kit.ctx) as uow:
        uow.conversations.add(inconsistent)
        uow.commit()  # the deferred counter check fails here
    assert err.value.code == codes.PERSISTENCE_FAILED and err.value.__cause__ is None
    with pytest.raises(NotFoundError):
        kit.read.execute(kit.ctx, ConversationReference(inconsistent.id))


def test_another_tenants_objects_are_refused_by_the_repository(
    sql_factory: SqlConversationsUnitOfWorkFactory,
) -> None:
    kit = build_kit(sql_factory)
    foreign = Conversation.start(
        id=ConversationId(uuid4()), tenant_id=new_tenant(), owner=kit.owner, now=START
    )
    with sql_factory.begin(kit.ctx) as uow, pytest.raises(InfrastructureError) as err:
        uow.conversations.add(foreign)
    assert err.value.code == codes.TENANT_MISMATCH


# ---- atomic rollback with a failing audit write -----------------------------------------------
class _FailingAudit:
    def record(self, fact: AuditFact) -> None:
        raise InfrastructureError("audit_unavailable")


def failing_audit_kit(app_engine: Engine, original: Kit) -> Kit:
    wiring = make_wiring(
        app_engine, FakeClock(), SequentialIdGenerator(), lambda tx: _FailingAudit()
    )
    factory = SqlConversationsUnitOfWorkFactory(wiring.factory)
    limits = LIMITS
    return Kit(
        factory=factory,
        records=original.records,
        clock=original.clock,
        ids=original.ids,  # continue the original id sequence: no id collision
        start=StartConversation(factory, original.clock, original.ids),
        append_user=AppendUserMessage(factory, original.clock, limits),
        append_assistant=AppendAssistantTurn(factory, original.records, original.clock, limits),
        read=ReadConversation(factory),
        listing=ListConversations(factory, limits),
        delete=DeleteConversation(factory, original.clock),
        tenant=original.tenant,
        owner=original.owner,
        ctx=original.ctx,
    )


def test_when_the_audit_write_fails_nothing_else_commits(
    sql_kit: Kit, app_engine: Engine, admin_engine: Engine
) -> None:
    kit = sql_kit
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    kit.append_user.execute(
        kit.ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "keep")
    )
    broken = failing_audit_kit(app_engine, kit)
    with pytest.raises(InfrastructureError):
        broken.start.execute(broken.ctx, StartConversationCommand())
    with pytest.raises(InfrastructureError):
        broken.append_user.execute(
            broken.ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "lost")
        )
    with pytest.raises(InfrastructureError):
        broken.delete.execute(broken.ctx, ConversationReference(cid))
    t = kit.tenant.value
    assert (
        count(
            admin_engine,
            "SELECT count(*) FROM conversations.conversations WHERE tenant_id = :t",
            t=t,
        )
        == 1
    )
    assert (
        count(
            admin_engine,
            "SELECT message_count FROM conversations.conversations "
            "WHERE tenant_id = :t AND id = :c",
            c=cid.value,
            t=kit.tenant.value,
        )
        == 1
    )
    assert (
        count(
            admin_engine,
            "SELECT count(*) FROM conversations.messages "
            "WHERE tenant_id = :t AND conversation_id = :c",
            c=cid.value,
            t=kit.tenant.value,
        )
        == 1
    )
    assert (
        count(
            admin_engine,
            "SELECT count(*) FROM conversations.deletion_receipts WHERE tenant_id = :t",
            t=t,
        )
        == 0
    )


def test_when_the_receipt_cannot_be_written_the_delete_rolls_back(
    sql_kit: Kit, admin_engine: Engine
) -> None:
    kit = sql_kit
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    kit.append_user.execute(
        kit.ctx, AppendUserMessageCommand(cid, ClientMessageId(uuid4()), "keep")
    )
    run_admin(
        admin_engine,
        RECEIPT_INSERT,
        t=kit.tenant.value,
        c=cid.value,
    )
    with pytest.raises(InfrastructureError):
        kit.delete.execute(kit.ctx, ConversationReference(cid))
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 1


# ---- concurrency: serialized by the row lock, guarded by the constraints ----------------------
def run_parallel(calls: list[Callable[[], Any]]) -> list[Any]:
    def attempt(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except Exception as exc:  # collected and inspected by the caller
            return exc

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(attempt, calls))


def roomy(sql_factory: SqlConversationsUnitOfWorkFactory) -> Kit:
    return build_kit(sql_factory, ConversationLimits(50, 60, 5))


def say_command(
    cid: ConversationId, text: str, client: ClientMessageId | None = None
) -> AppendUserMessageCommand:
    return AppendUserMessageCommand(cid, client or ClientMessageId(uuid4()), text)


def test_concurrent_appends_get_distinct_contiguous_sequences(
    sql_factory: SqlConversationsUnitOfWorkFactory,
) -> None:
    kit = roomy(sql_factory)
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    results = run_parallel(
        [partial(kit.append_user.execute, kit.ctx, say_command(cid, f"m{n}")) for n in range(10)]
    )
    assert all(not isinstance(r, Exception) for r in results), results
    assert sorted(r.message.sequence for r in results) == list(range(1, 11))
    history = kit.read.execute(kit.ctx, ConversationReference(cid))
    assert history.conversation.message_count == 10 and len(history.messages) == 10


def test_concurrent_repeats_of_one_client_message_id_store_it_once(
    sql_factory: SqlConversationsUnitOfWorkFactory,
) -> None:
    kit = roomy(sql_factory)
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    command = say_command(cid, "same")
    results = run_parallel([partial(kit.append_user.execute, kit.ctx, command)] * 8)
    assert all(not isinstance(r, Exception) for r in results), results
    assert sorted(r.replayed for r in results) == [False] + [True] * 7
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 1


def test_concurrent_reuse_of_a_client_message_id_with_other_text_has_one_winner(
    sql_factory: SqlConversationsUnitOfWorkFactory,
) -> None:
    kit = roomy(sql_factory)
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    client = ClientMessageId(uuid4())
    results = run_parallel(
        [
            partial(kit.append_user.execute, kit.ctx, say_command(cid, f"text {n}", client))
            for n in range(6)
        ]
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, Exception)]
    assert len(wins) == 1 and len(losses) == 5
    assert all(isinstance(e, ConflictError) and e.code == MESSAGE_CONFLICT for e in losses)
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 1


def test_one_record_cannot_be_linked_to_two_conversations_even_concurrently(
    sql_factory: SqlConversationsUnitOfWorkFactory,
) -> None:
    kit = roomy(sql_factory)
    first = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    second = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    record = a_record(kit.tenant, kit.owner)
    kit.records.add(record)
    results = run_parallel(
        [
            partial(
                kit.append_assistant.execute,
                kit.ctx,
                AppendAssistantTurnCommand(c, record.record_id),
            )
            for c in (first, second)
        ]
    )
    wins = [r for r in results if not isinstance(r, Exception)]
    losses = [r for r in results if isinstance(r, Exception)]
    assert len(wins) == 1 and len(losses) == 1
    assert isinstance(losses[0], ConflictError) and losses[0].code == RECORD_LINKED
    counts = [
        kit.read.execute(kit.ctx, ConversationReference(c)).conversation.message_count
        for c in (first, second)
    ]
    assert sorted(counts) == [0, 1]


def test_an_append_racing_a_delete_leaves_no_orphan_and_a_truthful_receipt(
    sql_factory: SqlConversationsUnitOfWorkFactory, admin_engine: Engine
) -> None:
    kit = roomy(sql_factory)
    for _ in range(6):
        cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
        deleted, appended = run_parallel(
            [
                partial(kit.delete.execute, kit.ctx, ConversationReference(cid)),
                partial(kit.append_user.execute, kit.ctx, say_command(cid, "x")),
            ]
        )
        assert not isinstance(deleted, Exception), deleted
        where = {"t": kit.tenant.value, "c": cid.value}
        assert (
            count(
                admin_engine,
                "SELECT count(*) FROM conversations.conversations WHERE tenant_id = :t AND id = :c",
                **where,
            )
            == 0
        )
        assert (
            count(
                admin_engine,
                "SELECT count(*) FROM conversations.messages "
                "WHERE tenant_id = :t AND conversation_id = :c",
                **where,
            )
            == 0
        )
        if isinstance(appended, Exception):
            assert isinstance(appended, NotFoundError) and appended.code == NOT_FOUND
            assert deleted.messages_deleted == 0
        else:
            assert deleted.messages_deleted == 1  # the receipt counts what was really removed


def test_the_limit_holds_under_concurrency(
    sql_factory: SqlConversationsUnitOfWorkFactory,
) -> None:
    kit = build_kit(sql_factory, ConversationLimits(3, 60, 3))
    cid = kit.start.execute(kit.ctx, StartConversationCommand()).conversation_id
    results = run_parallel(
        [partial(kit.append_user.execute, kit.ctx, say_command(cid, f"x{n}")) for n in range(8)]
    )
    assert sum(1 for r in results if not isinstance(r, Exception)) == 3
    assert all(isinstance(r, ConflictError) for r in results if isinstance(r, Exception))
    assert kit.read.execute(kit.ctx, ConversationReference(cid)).conversation.message_count == 3
