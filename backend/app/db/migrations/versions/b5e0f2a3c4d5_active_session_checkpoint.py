"""снимки активных занятий для восстановления

Revision ID: b5e0f2a3c4d5
Revises: a4c9d1e2f3b4
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b5e0f2a3c4d5"
down_revision = "a4c9d1e2f3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("live_state", postgresql.JSONB(), nullable=True))
    op.add_column(
        "sessions",
        sa.Column("checkpoint_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_sessions_active_checkpoint",
        "sessions",
        ["checkpoint_at"],
        unique=False,
        postgresql_where=sa.text("ended_at IS NULL AND live_state IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_active_checkpoint", table_name="sessions")
    op.drop_column("sessions", "checkpoint_at")
    op.drop_column("sessions", "live_state")
