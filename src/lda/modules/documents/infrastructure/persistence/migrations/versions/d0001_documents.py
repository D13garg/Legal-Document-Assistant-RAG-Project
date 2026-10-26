"""documents schema: documents, versions, upload_intents, purge_receipts (ADR-005/015/023).

Revision ID: d0001
Revises:
"""

from alembic import context, op

revision = "d0001"
down_revision = None
branch_labels = ("documents",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
TABLES = ("documents", "versions", "upload_intents", "purge_receipts")


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA documents")
    op.execute(
        r"""
        CREATE TABLE documents.documents (
            tenant_id uuid NOT NULL,
            document_id uuid NOT NULL,
            title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 255),
            status text NOT NULL CHECK (status IN ('active', 'deleted')),
            created_by uuid,
            created_at timestamptz NOT NULL,
            latest_version_number integer NOT NULL CHECK (latest_version_number >= 1),
            deleted_at timestamptz,
            PRIMARY KEY (tenant_id, document_id),
            CHECK ((status = 'deleted') = (deleted_at IS NOT NULL))
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE documents.versions (
            tenant_id uuid NOT NULL,
            version_id uuid NOT NULL,
            document_id uuid NOT NULL,
            number integer NOT NULL CHECK (number >= 1),
            status text NOT NULL CHECK (status IN ('pending')),
            media_type text NOT NULL CHECK (
                char_length(media_type) <= 127
                AND media_type ~ '^[a-z0-9][a-z0-9.+-]{0,62}/[a-z0-9][a-z0-9.+-]{0,62}$'
            ),
            size_bytes bigint NOT NULL CHECK (size_bytes >= 1),
            created_by uuid,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (tenant_id, version_id),
            UNIQUE (tenant_id, document_id, number),
            FOREIGN KEY (tenant_id, document_id)
                REFERENCES documents.documents (tenant_id, document_id) ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """
        CREATE TABLE documents.upload_intents (
            tenant_id uuid NOT NULL,
            version_id uuid NOT NULL,
            document_id uuid NOT NULL,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (tenant_id, version_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE documents.purge_receipts (
            tenant_id uuid NOT NULL,
            document_id uuid NOT NULL,
            tombstoned_at timestamptz NOT NULL,
            purged_at timestamptz NOT NULL,
            versions_purged integer NOT NULL CHECK (versions_purged >= 0),
            version_ids jsonb NOT NULL CHECK (jsonb_typeof(version_ids) = 'array'),
            scope text NOT NULL CHECK (scope = 'documents.originals_and_rows'),
            PRIMARY KEY (tenant_id, document_id)
        )
        """
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE documents.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE documents.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON documents.{table} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )
    # A document only gains versions (counter +1, under its row lock) and is tombstoned once,
    # irreversibly; a tombstoned row never changes again and is deleted only by purge.
    op.execute(
        """
        CREATE FUNCTION documents.guard_document_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status = 'deleted' THEN
                RAISE EXCEPTION 'a tombstoned document is immutable' USING ERRCODE = '55000';
            END IF;
            IF NEW.tenant_id <> OLD.tenant_id OR NEW.document_id <> OLD.document_id
               OR NEW.title <> OLD.title OR NEW.created_at <> OLD.created_at
               OR (NEW.created_by, NEW.tenant_id) IS DISTINCT FROM (OLD.created_by, OLD.tenant_id)
               THEN
                RAISE EXCEPTION 'document identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF NEW.latest_version_number NOT IN (OLD.latest_version_number,
                                                 OLD.latest_version_number + 1) THEN
                RAISE EXCEPTION 'the version counter only steps by one' USING ERRCODE = '55000';
            END IF;
            IF NEW.status = 'deleted'
               AND NEW.latest_version_number <> OLD.latest_version_number THEN
                RAISE EXCEPTION 'a tombstone adds no version' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER documents_guard_update BEFORE UPDATE ON documents.documents "
        "FOR EACH ROW EXECUTE FUNCTION documents.guard_document_update()"
    )
    op.execute(
        """
        CREATE FUNCTION documents.guard_document_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'deleted' THEN
                RAISE EXCEPTION 'only a tombstoned document can be purged' USING ERRCODE = '55000';
            END IF;
            RETURN OLD;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER documents_guard_delete BEFORE DELETE ON documents.documents "
        "FOR EACH ROW EXECUTE FUNCTION documents.guard_document_delete()"
    )
    # Versions are immutable: nothing but `status` may change (and the CHECK admits only
    # 'pending' until step 6), and a version is removed only by the purge of its document.
    op.execute(
        """
        CREATE FUNCTION documents.guard_version_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.tenant_id <> OLD.tenant_id OR NEW.version_id <> OLD.version_id
               OR NEW.document_id <> OLD.document_id OR NEW.number <> OLD.number
               OR NEW.media_type <> OLD.media_type OR NEW.size_bytes <> OLD.size_bytes
               OR (NEW.created_by, NEW.tenant_id) IS DISTINCT FROM (OLD.created_by, OLD.tenant_id)
               OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION 'document versions are immutable' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER versions_guard_update BEFORE UPDATE ON documents.versions "
        "FOR EACH ROW EXECUTE FUNCTION documents.guard_version_update()"
    )
    op.execute(
        """
        CREATE FUNCTION documents.guard_version_delete() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM documents.documents d
                WHERE d.tenant_id = OLD.tenant_id AND d.document_id = OLD.document_id
                  AND d.status = 'deleted'
            ) THEN
                RAISE EXCEPTION 'versions are removed only by purging a tombstoned document'
                    USING ERRCODE = '55000';
            END IF;
            RETURN OLD;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER versions_guard_delete BEFORE DELETE ON documents.versions "
        "FOR EACH ROW EXECUTE FUNCTION documents.guard_version_delete()"
    )
    # An upload intent is never changed. UPDATE is granted only because `SELECT ... FOR UPDATE`
    # (the lock that serializes the reaper against the upload's own commit) requires it.
    op.execute(
        """
        CREATE FUNCTION documents.reject_intent_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'upload intents are immutable' USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER intents_no_update BEFORE UPDATE ON documents.upload_intents "
        "FOR EACH ROW EXECUTE FUNCTION documents.reject_intent_update()"
    )
    # Purge receipts are evidence: insert-only for every role, and TRUNCATE is blocked.
    op.execute(
        """
        CREATE FUNCTION documents.reject_receipt_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'purge receipts are insert-only' USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER receipts_no_update_delete "
        "BEFORE UPDATE OR DELETE ON documents.purge_receipts "
        "FOR EACH ROW EXECUTE FUNCTION documents.reject_receipt_change()"
    )
    op.execute(
        "CREATE TRIGGER receipts_no_truncate BEFORE TRUNCATE ON documents.purge_receipts "
        "FOR EACH STATEMENT EXECUTE FUNCTION documents.reject_receipt_change()"
    )
    op.execute(f"GRANT USAGE ON SCHEMA documents TO {role}")
    # Least privilege: versions are never updated in step 5 (step 6 grants UPDATE (status)).
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON documents.documents TO {role}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON documents.versions TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON documents.upload_intents TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON documents.purge_receipts TO {role}")


def downgrade() -> None:
    op.execute("DROP SCHEMA documents CASCADE")
