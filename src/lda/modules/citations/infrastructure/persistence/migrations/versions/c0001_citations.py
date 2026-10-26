"""citations schema: generation_records and generation_record_citations (ADR-016).

Revision ID: c0001
Revises:

Identifiers, hashes, labels and counts only: never a prompt, answer, question, excerpt, chunk text
or authorization proof. Forced RLS on both tables, no cross-schema reference (the cited document,
version and chunk ids are opaque values, not foreign keys). Both tables are insert-only for every
role: no UPDATE/DELETE grant, row triggers that reject UPDATE and DELETE, and a TRUNCATE trigger.
A deferred constraint trigger makes a record and its citations all-or-nothing: the stored citation
rows must equal `citation_count`, and an answered record needs at least one (invariant 5). There
is deliberately no purge or retention path (ADR-016 decision 7; nothing here deletes).
"""

from alembic import context, op

revision = "c0001"
down_revision = None
branch_labels = ("citations",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
TABLES = ("generation_records", "generation_record_citations")


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA citations")
    op.execute(
        r"""
        CREATE TABLE citations.generation_records (
            tenant_id uuid NOT NULL,
            record_id uuid NOT NULL,
            principal_id uuid NOT NULL,
            request_id uuid NOT NULL,
            created_at timestamptz NOT NULL,
            config_id text NOT NULL CHECK (config_id ~ '^rag-[0-9a-f]{64}$'),
            outcome text NOT NULL
                CHECK (outcome IN ('answered', 'insufficient_evidence', 'rejected')),
            template_id text NOT NULL CHECK (template_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'),
            template_version text NOT NULL
                CHECK (template_version ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'),
            model_id text NOT NULL CHECK (model_id ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'),
            request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
            evidence_fingerprint text NOT NULL CHECK (evidence_fingerprint ~ '^[0-9a-f]{64}$'),
            citation_count integer NOT NULL CHECK (citation_count BETWEEN 0 AND 500),
            PRIMARY KEY (tenant_id, record_id),
            CHECK ((outcome = 'answered') = (citation_count > 0))
        )
        """
    )
    op.execute(
        r"""
        CREATE TABLE citations.generation_record_citations (
            tenant_id uuid NOT NULL,
            record_id uuid NOT NULL,
            position integer NOT NULL CHECK (position BETWEEN 0 AND 499),
            marker text NOT NULL CHECK (marker ~ '^S[1-9][0-9]{0,3}$'),
            document_id uuid NOT NULL,
            version_id uuid NOT NULL,
            chunk_id uuid NOT NULL,
            ordinal integer NOT NULL CHECK (ordinal BETWEEN 0 AND 1000000),
            text_hash text NOT NULL CHECK (text_hash ~ '^[0-9a-f]{64}$'),
            page_start integer CHECK (page_start >= 1),
            page_end integer CHECK (page_end >= 1),
            PRIMARY KEY (tenant_id, record_id, marker),
            UNIQUE (tenant_id, record_id, position),
            FOREIGN KEY (tenant_id, record_id)
                REFERENCES citations.generation_records (tenant_id, record_id),
            CHECK ((page_start IS NULL) = (page_end IS NULL)),
            CHECK (page_start IS NULL OR page_start <= page_end)
        )
        """
    )
    op.execute(
        "CREATE INDEX generation_record_citations_document "
        "ON citations.generation_record_citations (tenant_id, document_id)"
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE citations.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE citations.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON citations.{table} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )
    op.execute(
        """
        CREATE FUNCTION citations.reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'generation records are insert-only' USING ERRCODE = '55000';
        END
        $$
        """
    )
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE "
            f"ON citations.{table} FOR EACH ROW EXECUTE FUNCTION citations.reject_change()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON citations.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION citations.reject_change()"
        )
    op.execute(
        """
        CREATE FUNCTION citations.check_citation_count() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            expected integer;
            actual integer;
        BEGIN
            SELECT citation_count INTO expected FROM citations.generation_records
            WHERE tenant_id = NEW.tenant_id AND record_id = NEW.record_id;
            SELECT count(*) INTO actual FROM citations.generation_record_citations
            WHERE tenant_id = NEW.tenant_id AND record_id = NEW.record_id;
            IF expected IS DISTINCT FROM actual THEN
                RAISE EXCEPTION 'generation record citations are incomplete'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    for table in TABLES:
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {table}_complete AFTER INSERT ON citations.{table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION citations.check_citation_count()"
        )
    op.execute(f"GRANT USAGE ON SCHEMA citations TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON citations.generation_records TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON citations.generation_record_citations TO {role}")


def downgrade() -> None:
    op.execute("DROP SCHEMA citations CASCADE")
