"""evaluation schema: evaluation_runs and release_eligibility (ADR-041 D3).

Revision ID: v0001
Revises:

GLOBAL, not tenant-scoped, and deliberately without row-level security: the rows describe
synthetic evaluation runs and hold identifiers, hashes, counts and scores only (never a tenant,
principal, document, question, answer or secret). Both tables are insert-only for every role: no
UPDATE/DELETE grant, row triggers that reject UPDATE and DELETE, and a TRUNCATE trigger. The
database, not the application, enforces:
- the stored run hash equals the SHA-256 of the stored canonical text;
- every searchable column equals the value inside that text;
- an eligibility record can exist only for a run that exists, passed, and has the same
  configuration id, run hash and dataset fingerprint (composite foreign key);
- one eligibility record per run (unique run_id).
Superseding is an INSERT of a newer row; nothing is ever updated or removed. There is no purge or
retention path in this step.
"""

from alembic import context, op

revision = "v0001"
down_revision = None
branch_labels = ("evaluation",)
depends_on = None

TABLES = ("evaluation_runs", "release_eligibility")
HEX = "'^[0-9a-f]{64}$'"


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA evaluation")
    op.execute(
        rf"""
        CREATE TABLE evaluation.evaluation_runs (
            sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id uuid NOT NULL UNIQUE,
            config_id text NOT NULL CHECK (config_id ~ '^rag-[0-9a-f]{{64}}$'),
            dataset_id text NOT NULL
                CHECK (dataset_id ~ '^[A-Za-z0-9][A-Za-z0-9._:#/-]{{0,127}}$'),
            dataset_version integer NOT NULL CHECK (dataset_version >= 1),
            dataset_fingerprint text NOT NULL CHECK (dataset_fingerprint ~ {HEX}),
            evaluated_at bigint NOT NULL CHECK (evaluated_at >= 0),
            passed boolean NOT NULL,
            record_text text NOT NULL CHECK (octet_length(record_text) <= 4194304),
            record_sha256 text NOT NULL CHECK (record_sha256 ~ {HEX}),
            CHECK (record_sha256 = encode(sha256(convert_to(record_text, 'UTF8')), 'hex')),
            CHECK (record_text::jsonb ->> 'schema' = 'evaluation.run_record.v1'),
            CHECK (record_text::jsonb ->> 'run_id' = run_id::text),
            CHECK (record_text::jsonb #>> '{{configuration,config_id}}' = config_id),
            CHECK (record_text::jsonb #>> '{{dataset,id}}' = dataset_id),
            CHECK ((record_text::jsonb #>> '{{dataset,version}}')::integer = dataset_version),
            CHECK (record_text::jsonb #>> '{{dataset,fingerprint}}' = dataset_fingerprint),
            CHECK ((record_text::jsonb ->> 'evaluated_at')::bigint = evaluated_at),
            CHECK ((record_text::jsonb #>> '{{report,passed}}')::boolean = passed),
            UNIQUE (run_id, config_id, record_sha256, dataset_fingerprint, passed)
        )
        """
    )
    op.execute(
        "CREATE INDEX evaluation_runs_lookup ON evaluation.evaluation_runs "
        "(config_id, dataset_id, dataset_version, sequence DESC)"
    )
    op.execute(
        rf"""
        CREATE TABLE evaluation.release_eligibility (
            sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            eligibility_id uuid NOT NULL UNIQUE,
            run_id uuid NOT NULL UNIQUE,
            config_id text NOT NULL CHECK (config_id ~ '^rag-[0-9a-f]{{64}}$'),
            dataset_fingerprint text NOT NULL CHECK (dataset_fingerprint ~ {HEX}),
            run_sha256 text NOT NULL CHECK (run_sha256 ~ {HEX}),
            run_passed boolean NOT NULL CHECK (run_passed),
            recorded_at bigint NOT NULL CHECK (recorded_at >= 0),
            FOREIGN KEY (run_id, config_id, run_sha256, dataset_fingerprint, run_passed)
                REFERENCES evaluation.evaluation_runs
                    (run_id, config_id, record_sha256, dataset_fingerprint, passed)
        )
        """
    )
    op.execute(
        "CREATE INDEX release_eligibility_lookup ON evaluation.release_eligibility "
        "(config_id, sequence DESC)"
    )
    op.execute(
        """
        CREATE FUNCTION evaluation.reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'evaluation records are insert-only' USING ERRCODE = '55000';
        END
        $$
        """
    )
    for table in TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE "
            f"ON evaluation.{table} FOR EACH ROW EXECUTE FUNCTION evaluation.reject_change()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON evaluation.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION evaluation.reject_change()"
        )
    op.execute(f"GRANT USAGE ON SCHEMA evaluation TO {role}")
    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT ON evaluation.{table} TO {role}")


def downgrade() -> None:
    op.execute("DROP SCHEMA evaluation CASCADE")
