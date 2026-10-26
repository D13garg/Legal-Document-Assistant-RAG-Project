"""chunking schema: chunk_sets and purge_receipts (ADR-038).

Revision ID: k0001
Revises:

Identifiers, hashes, a chunker version and counts only: never the chunk text (it lives in the
object store). Forced RLS, no cross-schema reference. A chunk set is immutable (no UPDATE grant and
a trigger that rejects it for every role) and is deleted only by the purge flow. A purge receipt
is insert-only for every role and TRUNCATE is blocked.
"""

from alembic import context, op

revision = "k0001"
down_revision = None
branch_labels = ("chunking",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
TABLES = ("chunk_sets", "purge_receipts")


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA chunking")
    op.execute(
        r"""
        CREATE TABLE chunking.chunk_sets (
            tenant_id uuid NOT NULL,
            version_id uuid NOT NULL,
            document_id uuid NOT NULL,
            extraction_hash text NOT NULL CHECK (extraction_hash ~ '^[0-9a-f]{64}$'),
            chunker_version text NOT NULL CHECK (chunker_version ~ '^[A-Za-z0-9._+/ ;=-]{1,200}$'),
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            artifact_bytes bigint NOT NULL CHECK (artifact_bytes >= 1),
            chunk_count integer NOT NULL CHECK (chunk_count >= 1),
            character_count bigint NOT NULL CHECK (character_count >= 1),
            created_at timestamptz NOT NULL,
            PRIMARY KEY (tenant_id, version_id)
        )
        """
    )
    op.execute("CREATE INDEX chunk_sets_document ON chunking.chunk_sets (tenant_id, document_id)")
    op.execute(
        """
        CREATE TABLE chunking.purge_receipts (
            tenant_id uuid NOT NULL,
            document_id uuid NOT NULL,
            purged_at timestamptz NOT NULL,
            versions_purged integer NOT NULL CHECK (versions_purged >= 0),
            version_ids jsonb NOT NULL CHECK (jsonb_typeof(version_ids) = 'array'),
            scope text NOT NULL CHECK (scope = 'chunking.chunk_artifacts_and_rows'),
            PRIMARY KEY (tenant_id, document_id)
        )
        """
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE chunking.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE chunking.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON chunking.{table} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )
    op.execute(
        """
        CREATE FUNCTION chunking.reject_chunk_set_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'chunk sets are immutable' USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER chunk_sets_no_update BEFORE UPDATE ON chunking.chunk_sets "
        "FOR EACH ROW EXECUTE FUNCTION chunking.reject_chunk_set_update()"
    )
    op.execute(
        """
        CREATE FUNCTION chunking.reject_receipt_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'purge receipts are insert-only' USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER receipts_no_update_delete "
        "BEFORE UPDATE OR DELETE ON chunking.purge_receipts "
        "FOR EACH ROW EXECUTE FUNCTION chunking.reject_receipt_change()"
    )
    op.execute(
        "CREATE TRIGGER receipts_no_truncate BEFORE TRUNCATE ON chunking.purge_receipts "
        "FOR EACH STATEMENT EXECUTE FUNCTION chunking.reject_receipt_change()"
    )
    op.execute(f"GRANT USAGE ON SCHEMA chunking TO {role}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON chunking.chunk_sets TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON chunking.purge_receipts TO {role}")


def downgrade() -> None:
    op.execute("DROP SCHEMA chunking CASCADE")
