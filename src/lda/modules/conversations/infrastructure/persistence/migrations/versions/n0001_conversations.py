"""conversations schema: conversations, messages and deletion receipts (ADR-039, ADR-013).

Revision ID: n0001
Revises:
Branch Labels: conversations
"""

from __future__ import annotations

from alembic import context, op

revision = "n0001"
down_revision = None
branch_labels = ("conversations",)
depends_on = None

_TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_DELETE_SCOPE = "conversations.messages_and_rows"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE conversations.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE conversations.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON conversations.{table} "
        f"USING (tenant_id = {_TENANT}) WITH CHECK (tenant_id = {_TENANT})"
    )


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA conversations")

    op.execute(
        """
        CREATE TABLE conversations.conversations (
            tenant_id uuid NOT NULL,
            id uuid NOT NULL,
            owner_id uuid NOT NULL,
            created_at timestamptz NOT NULL,
            message_count integer NOT NULL,
            CONSTRAINT conversations_pk PRIMARY KEY (tenant_id, id),
            CONSTRAINT conversations_count_bounded CHECK (message_count BETWEEN 0 AND 1000)
        )
        """
    )
    op.execute(
        "CREATE INDEX conversations_owner_recent ON conversations.conversations "
        "(tenant_id, owner_id, created_at DESC, id DESC)"
    )
    op.execute(
        """
        CREATE TABLE conversations.messages (
            tenant_id uuid NOT NULL,
            conversation_id uuid NOT NULL,
            sequence integer NOT NULL,
            role text NOT NULL,
            created_at timestamptz NOT NULL,
            client_message_id uuid,
            text text,
            record_id uuid,
            CONSTRAINT messages_pk PRIMARY KEY (tenant_id, conversation_id, sequence),
            CONSTRAINT messages_conversation_fk FOREIGN KEY (tenant_id, conversation_id)
                REFERENCES conversations.conversations (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT messages_sequence_bounded CHECK (sequence BETWEEN 1 AND 1000),
            CONSTRAINT messages_text_bounded CHECK (
                text IS NULL OR (btrim(text) <> '' AND char_length(text) <= 32000)
            ),
            -- A user message has text and a client id; an assistant message has ONLY a record id.
            -- An assistant answer cannot be stored here (ADR-039 C1).
            CONSTRAINT messages_role_shape CHECK (
                (role = 'user' AND client_message_id IS NOT NULL AND text IS NOT NULL
                    AND record_id IS NULL)
                OR (role = 'assistant' AND record_id IS NOT NULL AND text IS NULL
                    AND client_message_id IS NULL)
            ),
            CONSTRAINT messages_client_message_unique
                UNIQUE (tenant_id, conversation_id, client_message_id),
            CONSTRAINT messages_record_unique UNIQUE (tenant_id, record_id)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE conversations.deletion_receipts (
            tenant_id uuid NOT NULL,
            conversation_id uuid NOT NULL,
            deleted_at timestamptz NOT NULL,
            messages_deleted integer NOT NULL,
            scope text NOT NULL,
            CONSTRAINT deletion_receipts_pk PRIMARY KEY (tenant_id, conversation_id),
            CONSTRAINT deletion_receipts_count_bounded CHECK (messages_deleted BETWEEN 0 AND 1000),
            CONSTRAINT deletion_receipts_scope CHECK (scope = '{_DELETE_SCOPE}')
        )
        """
    )
    for table in ("conversations", "messages", "deletion_receipts"):
        _rls(table)

    # ---- immutability, enforced for every role ------------------------------------------------
    op.execute(
        """
        CREATE FUNCTION conversations.refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'conversations: % on % is not allowed', TG_OP, TG_TABLE_NAME
                USING ERRCODE = '55000';
        END $$
        """
    )
    for table in ("messages", "deletion_receipts"):
        op.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON conversations.{table} "
            "FOR EACH ROW EXECUTE FUNCTION conversations.refuse_change()"
        )
    op.execute(
        "CREATE TRIGGER deletion_receipts_no_delete BEFORE DELETE "
        "ON conversations.deletion_receipts "
        "FOR EACH ROW EXECUTE FUNCTION conversations.refuse_change()"
    )
    for table in ("conversations", "messages", "deletion_receipts"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON conversations.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION conversations.refuse_change()"
        )
    # A message may be deleted only once its conversation is gone (the ON DELETE CASCADE path).
    op.execute(
        """
        CREATE FUNCTION conversations.messages_delete_with_parent() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM conversations.conversations c
                WHERE c.tenant_id = OLD.tenant_id AND c.id = OLD.conversation_id
            ) THEN
                RAISE EXCEPTION 'conversations: messages are immutable' USING ERRCODE = '55000';
            END IF;
            RETURN OLD;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER messages_delete_only_with_parent BEFORE DELETE ON conversations.messages "
        "FOR EACH ROW EXECUTE FUNCTION conversations.messages_delete_with_parent()"
    )
    # A conversation may only grow by exactly one message at a time; nothing else may change.
    op.execute(
        """
        CREATE FUNCTION conversations.conversation_update_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.tenant_id <> NEW.tenant_id OR OLD.id <> NEW.id
               OR OLD.owner_id <> NEW.owner_id
               OR OLD.created_at <> NEW.created_at
               OR NEW.message_count <> OLD.message_count + 1 THEN
                RAISE EXCEPTION 'conversations: only a one-step message counter change is allowed'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER conversations_update_guard BEFORE UPDATE ON conversations.conversations "
        "FOR EACH ROW EXECUTE FUNCTION conversations.conversation_update_guard()"
    )
    # At commit, counter, message count and sequence run must agree (checked from both sides).
    op.execute(
        """
        CREATE FUNCTION conversations.check_counter(t uuid, c uuid) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE n integer; have integer; top integer;
        BEGIN
            SELECT message_count INTO n FROM conversations.conversations
                WHERE tenant_id = t AND id = c;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT count(*), COALESCE(max(sequence), 0) INTO have, top
                FROM conversations.messages WHERE tenant_id = t AND conversation_id = c;
            IF have <> n OR top <> n THEN
                RAISE EXCEPTION 'conversations: message counter is inconsistent'
                    USING ERRCODE = '23514';
            END IF;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION conversations.messages_counter_check() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM conversations.check_counter(NEW.tenant_id, NEW.conversation_id);
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION conversations.conversations_counter_check() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM conversations.check_counter(NEW.tenant_id, NEW.id);
            RETURN NULL;
        END $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER messages_counter_consistent "
        "AFTER INSERT ON conversations.messages "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION conversations.messages_counter_check()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER conversations_counter_consistent "
        "AFTER INSERT OR UPDATE ON conversations.conversations "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION conversations.conversations_counter_check()"
    )

    # ---- privileges ---------------------------------------------------------------------------
    op.execute(f"GRANT USAGE ON SCHEMA conversations TO {role}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON conversations.conversations TO {role}")
    op.execute(f"GRANT UPDATE (message_count) ON conversations.conversations TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON conversations.messages TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON conversations.deletion_receipts TO {role}")


def downgrade() -> None:
    op.execute("DROP SCHEMA conversations CASCADE")
