"""сохраняемая версия полномочий пользователя

Revision ID: c6f1a3b4d5e6
Revises: b5e0f2a3c4d5
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "c6f1a3b4d5e6"
down_revision = "b5e0f2a3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("auth_version", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "auth_version")
