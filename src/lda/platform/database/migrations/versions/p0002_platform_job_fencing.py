"""platform.jobs: fencing token for lease-owner-gated operations (ADR-012 amendment).

Revision ID: p0002
Revises: p0001
"""

from alembic import op

revision = "p0002"
down_revision = "p0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive: existing rows start at 0; every claim and every reclaim increases it.
    op.execute("ALTER TABLE platform.jobs ADD COLUMN fencing_token bigint NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE platform.jobs DROP COLUMN fencing_token")
