"""ingestion schema: runs and stage_runs (ADR-010).

Revision ID: g0001
Revises:

Identifiers, statuses, counters and durations only: never content. Forced RLS on both tables, no
cross-schema reference, and database triggers that repeat the state machine of the domain.
"""

from alembic import context, op

revision = "g0001"
down_revision = None
branch_labels = ("ingestion",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
CODE = "'^[a-z][a-z0-9_.]{0,63}$'"
TABLES = ("runs", "stage_runs")


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA ingestion")
    op.execute(
        f"""
        CREATE TABLE ingestion.runs (
            tenant_id uuid NOT NULL,
            run_id uuid NOT NULL,
            document_id uuid NOT NULL,
            version_id uuid NOT NULL,
            generation integer NOT NULL CHECK (generation >= 1),
            status text NOT NULL CHECK (status IN
                ('running', 'completed', 'failed', 'quarantined', 'dead_lettered', 'cancelled')),
            error_code text CHECK (error_code IS NULL OR error_code ~ {CODE}),
            created_at timestamptz NOT NULL,
            progress_at timestamptz NOT NULL,
            finished_at timestamptz,
            acknowledged_at timestamptz,
            redeliveries integer NOT NULL DEFAULT 0 CHECK (redeliveries >= 0),
            PRIMARY KEY (tenant_id, run_id),
            UNIQUE (tenant_id, version_id, generation),
            CHECK ((status = 'running') = (finished_at IS NULL)),
            CHECK (acknowledged_at IS NULL OR status IN ('completed', 'failed', 'quarantined'))
        )
        """
    )
    op.execute("CREATE INDEX runs_document ON ingestion.runs (tenant_id, document_id)")
    op.execute(
        f"""
        CREATE TABLE ingestion.stage_runs (
            tenant_id uuid NOT NULL,
            run_id uuid NOT NULL,
            stage text NOT NULL CHECK (stage ~ '^[a-z][a-z0-9_]{{0,63}}$'),
            position integer NOT NULL CHECK (position >= 0),
            status text NOT NULL CHECK (status IN
                ('pending', 'running', 'succeeded', 'failed', 'quarantined')),
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            started_at timestamptz,
            finished_at timestamptz,
            duration_ms integer CHECK (duration_ms IS NULL OR duration_ms >= 0),
            error_code text CHECK (error_code IS NULL OR error_code ~ {CODE}),
            PRIMARY KEY (tenant_id, run_id, stage),
            UNIQUE (tenant_id, run_id, position),
            FOREIGN KEY (tenant_id, run_id)
                REFERENCES ingestion.runs (tenant_id, run_id) ON DELETE CASCADE
        )
        """
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE ingestion.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE ingestion.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON ingestion.{table} "
            f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
        )
    # Run lifecycle: running -> completed | failed | quarantined | dead_lettered | cancelled and
    # dead_lettered -> cancelled. Nothing else moves. A finished run keeps its code and time; only
    # the delivery bookkeeping (acknowledgement, redeliveries, progress) may still change.
    op.execute(
        """
        CREATE FUNCTION ingestion.guard_run_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.tenant_id <> OLD.tenant_id OR NEW.run_id <> OLD.run_id
               OR NEW.document_id <> OLD.document_id OR NEW.version_id <> OLD.version_id
               OR NEW.generation <> OLD.generation OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION 'run identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF NEW.status <> OLD.status THEN
                IF NOT ((OLD.status = 'running' AND NEW.status IN
                            ('completed', 'failed', 'quarantined', 'dead_lettered', 'cancelled'))
                        OR (OLD.status = 'dead_lettered' AND NEW.status = 'cancelled')) THEN
                    RAISE EXCEPTION 'invalid run transition' USING ERRCODE = '55000';
                END IF;
            ELSIF OLD.status <> 'running'
                  AND (NEW.error_code, NEW.finished_at)
                      IS DISTINCT FROM (OLD.error_code, OLD.finished_at) THEN
                RAISE EXCEPTION 'a finished run is immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.acknowledged_at IS NOT NULL
               AND (NEW.acknowledged_at) IS DISTINCT FROM (OLD.acknowledged_at) THEN
                RAISE EXCEPTION 'an acknowledgement is final' USING ERRCODE = '55000';
            END IF;
            IF NEW.redeliveries < OLD.redeliveries THEN
                RAISE EXCEPTION 'redeliveries never decrease' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER runs_guard_update BEFORE UPDATE ON ingestion.runs "
        "FOR EACH ROW EXECUTE FUNCTION ingestion.guard_run_update()"
    )
    # Stage lifecycle: pending -> running -> (pending again after a retryable failure | succeeded |
    # failed | quarantined); running -> running when a dead executor's stage starts again. A
    # finished stage never changes.
    op.execute(
        """
        CREATE FUNCTION ingestion.guard_stage_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.tenant_id <> OLD.tenant_id OR NEW.run_id <> OLD.run_id
               OR NEW.stage <> OLD.stage OR NEW.position <> OLD.position THEN
                RAISE EXCEPTION 'stage identity is immutable' USING ERRCODE = '55000';
            END IF;
            IF OLD.status IN ('succeeded', 'failed', 'quarantined') THEN
                IF NEW IS DISTINCT FROM OLD THEN
                    RAISE EXCEPTION 'a finished stage is immutable' USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.status <> OLD.status AND NOT (
                (OLD.status = 'pending' AND NEW.status = 'running')
                OR (OLD.status = 'running' AND NEW.status IN
                        ('pending', 'succeeded', 'failed', 'quarantined'))) THEN
                RAISE EXCEPTION 'invalid stage transition' USING ERRCODE = '55000';
            END IF;
            IF NEW.attempts < OLD.attempts THEN
                RAISE EXCEPTION 'stage attempts never decrease' USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER stage_runs_guard_update BEFORE UPDATE ON ingestion.stage_runs "
        "FOR EACH ROW EXECUTE FUNCTION ingestion.guard_stage_update()"
    )
    op.execute(f"GRANT USAGE ON SCHEMA ingestion TO {role}")
    for table in TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ingestion.{table} TO {role}")


def downgrade() -> None:
    op.execute("DROP SCHEMA ingestion CASCADE")
