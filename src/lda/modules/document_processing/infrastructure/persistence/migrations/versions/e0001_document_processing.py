"""document_processing schema: extractions and purge_receipts (ADR-037).

Revision ID: e0001
Revises:

Identifiers, a hash, a parser version and counts only: never the extracted text (it lives in the
object store). Forced RLS, no cross-schema reference. An extraction is immutable (no UPDATE grant
and a trigger that rejects it for every role) and is deleted only by the purge flow. A purge
receipt is insert-only for every role and TRUNCATE is blocked.
"""

from alembic import context, op

revision = "e0001"
down_revision = None
branch_labels = ("document_processing",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
TABLES = ("extractions", "purge_receipts")


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA document_processing")
    op.execute(
        r"""
        CREATE TABLE document_processing.extractions (
            tenant_id uuid NOT NULL,
            version_id uuid NOT NULL,
            document_id uuid NOT NULL,
            parser_version text NOT NULL CHECK (parser_version ~ '^[A-Za-z0-9._+/ ;=-]{1,200}$'),
            content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
            media_type text NOT NULL CHECK (
                char_length(media_type) <= 127
                AND media_type ~ '^[a-z0-9][a-z0-9.+-]{0,62}/[a-z0-9][a-z0-9.+-]{0,62}$'
            ),
            artifact_bytes bigint NOT NULL CHECK (artifact_bytes >= 1),
            block_count integer NOT NULL CHECK (block_count >= 1),
            character_count bigint NOT NULL CHECK (character_count >= 1),
            page_count integer CHECK (page_count IS NULL OR page_count >= 1),
            created_at timestamptz NOT NULL,
            PRIMARY KEY (tenant_id, version_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX extractions_document ON document_processing.extractions "
        "(tenant_id, document_id)"
    )
    op.execute(
        """
        CREATE TABLE document_processing.purge_receipts (
            tenant_id uuid NOT NULL,
            document_id uuid NOT NULL,
            purged_at timestamptz NOT NULL,
            versions_purged integer NOT NULL CHECK (versions_purged >= 0),
            version_ids jsonb NOT NULL CHECK (jsonb_typeof(version_ids) = 'array'),
            scope text NOT NULL
                CHECK (scope = 'document_processing.extracted_artifacts_and_rows'),
            PRIMARY KEY (tenant_id, document_id)
        )
        """
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE document_processing.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE document_processing.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON document_processing.{table} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )
    op.execute(
        """
        CREATE FUNCTION document_processing.reject_extraction_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'extractions are immutable' USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER extractions_no_update BEFORE UPDATE ON document_processing.extractions "
        "FOR EACH ROW EXECUTE FUNCTION document_processing.reject_extraction_update()"
    )
    op.execute(
        """
        CREATE FUNCTION document_processing.reject_receipt_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'purge receipts are insert-only' USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER receipts_no_update_delete "
        "BEFORE UPDATE OR DELETE ON document_processing.purge_receipts "
        "FOR EACH ROW EXECUTE FUNCTION document_processing.reject_receipt_change()"
    )
    op.execute(
        "CREATE TRIGGER receipts_no_truncate BEFORE TRUNCATE ON document_processing.purge_receipts "
        "FOR EACH STATEMENT EXECUTE FUNCTION document_processing.reject_receipt_change()"
    )
    op.execute(f"GRANT USAGE ON SCHEMA document_processing TO {role}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON document_processing.extractions TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON document_processing.purge_receipts TO {role}")


def downgrade() -> None:
    op.execute("DROP SCHEMA document_processing CASCADE")
