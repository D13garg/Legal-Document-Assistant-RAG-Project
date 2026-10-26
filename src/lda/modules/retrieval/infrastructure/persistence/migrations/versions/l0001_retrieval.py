"""retrieval schema: index_versions, chunk_vectors and purge_receipts (ADR-004, ADR-007, ADR-008).

Revision ID: l0001
Revises:

Identifiers, hashes, an embedding identity, counts and the vectors themselves: never chunk text
(it stays in chunking's object store, ADR-038) and never a copy of any permission (the live
`AccessScope` filters every query, ADR-008). Forced RLS, no cross-schema reference. Index rows are
immutable (no UPDATE grant and a trigger that rejects it for every role) and are deleted only by
the purge flow. A purge receipt is insert-only for every role and TRUNCATE is blocked.

The pgvector extension is created in `public` (its operators and type are schema-qualified in every
statement, so no role needs `public` on its search path). Creating it needs a role allowed to; the
migration runs as the owner, never as the runtime role.
"""

from alembic import context, op

revision = "l0001"
down_revision = None
branch_labels = ("retrieval",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
TABLES = ("index_versions", "chunk_vectors", "purge_receipts")
HASH = "'^[0-9a-f]{64}$'"
NAME = "'^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$'"
CHUNKER = "'^[A-Za-z0-9._+/ ;=-]{1,200}$'"


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.execute("CREATE SCHEMA retrieval")
    op.execute(
        rf"""
        CREATE TABLE retrieval.index_versions (
            tenant_id uuid NOT NULL,
            version_id uuid NOT NULL,
            index_id text NOT NULL CHECK (index_id ~ '^[0-9a-f]{{32}}$'),
            document_id uuid NOT NULL,
            embedding_provider text NOT NULL CHECK (embedding_provider ~ {NAME}),
            embedding_model text NOT NULL CHECK (embedding_model ~ {NAME}),
            embedding_version text NOT NULL CHECK (embedding_version ~ {NAME}),
            dimensions integer NOT NULL CHECK (dimensions = 256),
            chunker_version text NOT NULL CHECK (chunker_version ~ {CHUNKER}),
            source_hash text NOT NULL CHECK (source_hash ~ {HASH}),
            chunk_count integer NOT NULL CHECK (chunk_count >= 1),
            created_at timestamptz NOT NULL,
            PRIMARY KEY (tenant_id, version_id, index_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX index_versions_document ON retrieval.index_versions (tenant_id, document_id)"
    )
    op.execute(
        rf"""
        CREATE TABLE retrieval.chunk_vectors (
            tenant_id uuid NOT NULL,
            index_id text NOT NULL CHECK (index_id ~ '^[0-9a-f]{{32}}$'),
            chunk_id uuid NOT NULL,
            document_id uuid NOT NULL,
            version_id uuid NOT NULL,
            ordinal integer NOT NULL CHECK (ordinal >= 0),
            text_hash text NOT NULL CHECK (text_hash ~ {HASH}),
            embedding public.vector(256) NOT NULL,
            PRIMARY KEY (tenant_id, index_id, chunk_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX chunk_vectors_document "
        "ON retrieval.chunk_vectors (tenant_id, index_id, document_id)"
    )
    op.execute(
        "CREATE INDEX chunk_vectors_purge ON retrieval.chunk_vectors (tenant_id, document_id)"
    )
    op.execute(
        """
        CREATE TABLE retrieval.purge_receipts (
            tenant_id uuid NOT NULL,
            document_id uuid NOT NULL,
            purged_at timestamptz NOT NULL,
            versions_purged integer NOT NULL CHECK (versions_purged >= 0),
            version_ids jsonb NOT NULL CHECK (jsonb_typeof(version_ids) = 'array'),
            scope text NOT NULL CHECK (scope = 'retrieval.vector_rows'),
            PRIMARY KEY (tenant_id, document_id)
        )
        """
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE retrieval.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE retrieval.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON retrieval.{table} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )
    op.execute(
        """
        CREATE FUNCTION retrieval.reject_index_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'index rows are immutable' USING ERRCODE = '55000';
        END
        $$
        """
    )
    for table in ("index_versions", "chunk_vectors"):
        op.execute(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON retrieval.{table} "
            "FOR EACH ROW EXECUTE FUNCTION retrieval.reject_index_update()"
        )
    op.execute(
        """
        CREATE FUNCTION retrieval.reject_receipt_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'purge receipts are insert-only' USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER receipts_no_update_delete "
        "BEFORE UPDATE OR DELETE ON retrieval.purge_receipts "
        "FOR EACH ROW EXECUTE FUNCTION retrieval.reject_receipt_change()"
    )
    op.execute(
        "CREATE TRIGGER receipts_no_truncate BEFORE TRUNCATE ON retrieval.purge_receipts "
        "FOR EACH STATEMENT EXECUTE FUNCTION retrieval.reject_receipt_change()"
    )
    op.execute(f"GRANT USAGE ON SCHEMA retrieval TO {role}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON retrieval.index_versions TO {role}")
    op.execute(f"GRANT SELECT, INSERT, DELETE ON retrieval.chunk_vectors TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON retrieval.purge_receipts TO {role}")


def downgrade() -> None:
    op.execute("DROP SCHEMA retrieval CASCADE")  # the shared extension is left in place
