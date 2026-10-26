"""audit.records: insert-only, tenant-scoped, forced RLS (ADR-013, ADR-019, ADR-027).

Revision ID: u0001
Revises:
"""

from alembic import context, op

revision = "u0001"
down_revision = None
branch_labels = ("audit",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA audit")
    op.execute(
        """
        CREATE FUNCTION audit.attributes_ok(a jsonb) RETURNS boolean
        LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE WHEN jsonb_typeof(a) = 'object'
                THEN (SELECT count(*) FROM jsonb_object_keys(a)) <= 16 ELSE false END
        $$
        """
    )
    # The primary key leads with tenant_id (tenant-leading index). No foreign keys: the actor is
    # an identifier only, Identity's data is never copied or referenced (ADR-027).
    op.execute(
        r"""
        CREATE TABLE audit.records (
            record_id bigint GENERATED ALWAYS AS IDENTITY,
            tenant_id uuid NOT NULL,
            actor uuid,
            action text NOT NULL CHECK (
                char_length(action) <= 128
                AND action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'
            ),
            resource_type text NOT NULL CHECK (resource_type ~ '^[a-z][a-z0-9_]{0,63}$'),
            resource_id text CHECK (resource_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
            outcome text NOT NULL CHECK (outcome IN ('success', 'denied', 'failure')),
            request_id uuid,
            occurred_at timestamptz NOT NULL,
            attributes jsonb NOT NULL CHECK (audit.attributes_ok(attributes)),
            PRIMARY KEY (tenant_id, record_id)
        )
        """
    )
    op.execute("ALTER TABLE audit.records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit.records FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY records_tenant ON audit.records "
        f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
    )
    # Insert-only (D7): updates and deletes are rejected for EVERY role, the owner included, and
    # TRUNCATE is blocked. Retention/deletion is deferred to ADR-023.
    op.execute(
        """
        CREATE FUNCTION audit.reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit records are insert-only' USING ERRCODE = '55000';
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER records_no_update_delete BEFORE UPDATE OR DELETE ON audit.records "
        "FOR EACH ROW EXECUTE FUNCTION audit.reject_change()"
    )
    op.execute(
        "CREATE TRIGGER records_no_truncate BEFORE TRUNCATE ON audit.records "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_change()"
    )
    op.execute(f"GRANT USAGE ON SCHEMA audit TO {role}")
    # The runtime role can append and nothing else: no read, no update, no delete (D7, D8).
    op.execute(f"GRANT INSERT ON audit.records TO {role}")


def downgrade() -> None:
    op.execute("DROP TABLE audit.records")
    op.execute("DROP FUNCTION audit.reject_change()")
    op.execute("DROP FUNCTION audit.attributes_ok(jsonb)")
    op.execute("DROP SCHEMA audit")
