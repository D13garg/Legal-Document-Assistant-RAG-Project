"""documents.versions: the terminal outcomes of ingestion (ADR-015 amendment, ADR-010 §7).

Revision ID: d0002
Revises: d0001

Documents (not ingestion) owns this machinery. A version leaves `pending` exactly once, to `ready`,
`failed` or `quarantined`; all three are terminal. Only `status` may be updated by the runtime role
(column-level grant) and a version of a tombstoned document can never become `ready`.
"""

from alembic import context, op

revision = "d0002"
down_revision = "d0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute("ALTER TABLE documents.versions DROP CONSTRAINT versions_status_check")
    op.execute(
        "ALTER TABLE documents.versions ADD CONSTRAINT versions_status_check "
        "CHECK (status IN ('pending', 'ready', 'failed', 'quarantined'))"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION documents.guard_version_update() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.tenant_id <> OLD.tenant_id OR NEW.version_id <> OLD.version_id
               OR NEW.document_id <> OLD.document_id OR NEW.number <> OLD.number
               OR NEW.media_type <> OLD.media_type OR NEW.size_bytes <> OLD.size_bytes
               OR (NEW.created_by, NEW.tenant_id) IS DISTINCT FROM (OLD.created_by, OLD.tenant_id)
               OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION 'document versions are immutable' USING ERRCODE = '55000';
            END IF;
            IF NEW.status <> OLD.status THEN
                IF OLD.status <> 'pending' THEN
                    RAISE EXCEPTION 'a version outcome is terminal' USING ERRCODE = '55000';
                END IF;
                IF NEW.status = 'ready' AND NOT EXISTS (
                    SELECT 1 FROM documents.documents d
                    WHERE d.tenant_id = OLD.tenant_id AND d.document_id = OLD.document_id
                      AND d.status = 'active'
                ) THEN
                    RAISE EXCEPTION 'a version of a deleted document cannot become ready'
                        USING ERRCODE = '55000';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(f"GRANT UPDATE (status) ON documents.versions TO {role}")


def downgrade() -> None:
    role = context.config.attributes["app_role"]
    op.execute(f"REVOKE UPDATE (status) ON documents.versions FROM {role}")
    op.execute("ALTER TABLE documents.versions DROP CONSTRAINT versions_status_check")
    op.execute(
        "ALTER TABLE documents.versions ADD CONSTRAINT versions_status_check "
        "CHECK (status IN ('pending'))"
    )
