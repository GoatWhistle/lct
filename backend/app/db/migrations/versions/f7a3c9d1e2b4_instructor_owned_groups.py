"""закрепить учебные группы за преподавателем

Revision ID: f7a3c9d1e2b4
Revises: d7e2f4a5b6c7
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "f7a3c9d1e2b4"
down_revision = "d7e2f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing groups are not auto-assigned: historical membership/analytics may
    # span instructors, so they remain admin-managed until explicitly migrated.
    op.add_column("groups", sa.Column("owner_login", sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column("groups", "owner_login")
