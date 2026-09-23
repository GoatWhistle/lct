"""ограничение авторского контента и занятий владельцем-преподавателем

Revision ID: d7e2f4a5b6c7
Revises: c6f1a3b4d5e6
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "d7e2f4a5b6c7"
down_revision = "c6f1a3b4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenarios",
        sa.Column("owner_login", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "sessions",
        sa.Column("owner_login", sa.String(length=80), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "owner_login")
    op.drop_column("scenarios", "owner_login")
