"""access_control.grants and policy_epochs: tenant-scoped, forced RLS (ADR-013, ADR-014, ADR-027).

Revision ID: a0001
Revises:
"""

from alembic import context, op

revision = "a0001"
down_revision = None
branch_labels = ("access_control",)
depends_on = None

TENANT = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def _tenant_table(table: str) -> None:
    op.execute(f"ALTER TABLE access_control.{table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE access_control.{table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant ON access_control.{table} "
        f"USING (tenant_id = {TENANT}) WITH CHECK (tenant_id = {TENANT})"
    )


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("CREATE SCHEMA access_control")
    # The primary key leads with tenant_id (tenant-leading index). No foreign keys: principal
    # existence is verified through the Identity API, never by constraint (ADR-027).
    op.execute(
        r"""
        CREATE TABLE access_control.grants (
            tenant_id uuid NOT NULL,
            principal_id uuid NOT NULL,
            resource_kind text NOT NULL CHECK (resource_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
            resource_id text NOT NULL
                CHECK (resource_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
            action text NOT NULL CHECK (
                char_length(action) <= 128
                AND action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$'
            ),
            granted_at timestamptz NOT NULL,
            PRIMARY KEY (tenant_id, principal_id, resource_kind, resource_id, action)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE access_control.policy_epochs (
            tenant_id uuid PRIMARY KEY,
            epoch bigint NOT NULL CHECK (epoch >= 1)
        )
        """
    )
    _tenant_table("grants")
    _tenant_table("policy_epochs")
    # The epoch is monotonic by construction: an update must add exactly one and keep the tenant.
    op.execute(
        """
        CREATE FUNCTION access_control.enforce_epoch_step() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.tenant_id <> OLD.tenant_id OR NEW.epoch <> OLD.epoch + 1 THEN
                RAISE EXCEPTION 'policy epoch must advance by exactly one'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER policy_epochs_step BEFORE UPDATE ON access_control.policy_epochs "
        "FOR EACH ROW EXECUTE FUNCTION access_control.enforce_epoch_step()"
    )
    op.execute(f"GRANT USAGE ON SCHEMA access_control TO {role}")
    # Grants are revoked by deleting the row; epochs are never deleted.
    op.execute(f"GRANT SELECT, INSERT, DELETE ON access_control.grants TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON access_control.policy_epochs TO {role}")


def downgrade() -> None:
    op.execute("DROP TABLE access_control.policy_epochs")
    op.execute("DROP TABLE access_control.grants")
    op.execute("DROP FUNCTION access_control.enforce_epoch_step()")
    op.execute("DROP SCHEMA access_control")
