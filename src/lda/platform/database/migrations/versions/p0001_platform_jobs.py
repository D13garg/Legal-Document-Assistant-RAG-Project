"""platform.jobs: transactional job queue with forced RLS (ADR-012, ADR-013).

Revision ID: p0001
Revises:
"""

from alembic import context, op

revision = "p0001"
down_revision = None
branch_labels = ("platform",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
SYSTEM = "NULLIF(current_setting('app.system_scope', true), '') = 'job_queue'"


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA platform")
    op.execute(
        """
        CREATE TABLE platform.jobs (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            queue text NOT NULL,
            job_type text NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'dead')),
            attempts integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL CHECK (max_attempts >= 1),
            run_at timestamptz NOT NULL,
            unique_key text,
            lease_owner text,
            lease_expires_at timestamptz,
            last_error_code text,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            finished_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX jobs_tenant_unique_key ON platform.jobs (tenant_id, unique_key) "
        "WHERE unique_key IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX jobs_claim ON platform.jobs (queue, run_at, created_at) "
        "WHERE status = 'queued'"
    )
    op.execute(
        "CREATE INDEX jobs_lease ON platform.jobs (lease_expires_at) WHERE status = 'running'"
    )
    op.execute("ALTER TABLE platform.jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE platform.jobs FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY jobs_tenant ON platform.jobs "
        f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
    )
    op.execute(
        f"CREATE POLICY jobs_system_queue ON platform.jobs USING ({SYSTEM}) WITH CHECK ({SYSTEM})"
    )
    op.execute(f"GRANT USAGE ON SCHEMA platform TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON platform.jobs TO {role}")


def downgrade() -> None:
    op.execute("DROP TABLE platform.jobs")
    op.execute("DROP SCHEMA platform")
