"""identity.principals: tenant-scoped registry with forced RLS (ADR-013, ADR-027, ADR-035).

Revision ID: i0001
Revises:
"""

from alembic import context, op

revision = "i0001"
down_revision = None
branch_labels = ("identity",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA identity")
    op.execute(
        """
        CREATE TABLE identity.principals (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            kind text NOT NULL CHECK (kind IN ('user', 'service')),
            display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
            status text NOT NULL CHECK (status IN ('active', 'disabled')),
            version integer NOT NULL CHECK (version >= 1),
            created_at timestamptz NOT NULL,
            status_changed_at timestamptz NOT NULL,
            CHECK (status_changed_at >= created_at)
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX principals_tenant_id ON identity.principals (tenant_id, id)")
    op.execute("ALTER TABLE identity.principals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE identity.principals FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY principals_tenant ON identity.principals "
        f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
    )
    op.execute(f"GRANT USAGE ON SCHEMA identity TO {role}")
    # Principals are disabled, never deleted: the runtime role gets no DELETE.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON identity.principals TO {role}")


def downgrade() -> None:
    op.execute("DROP TABLE identity.principals")
    op.execute("DROP SCHEMA identity")
