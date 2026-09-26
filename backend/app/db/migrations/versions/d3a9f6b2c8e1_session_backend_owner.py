"""persist backend node ownership for active sessions

Revision ID: d3a9f6b2c8e1
Revises: c2d7e9f4a1b6
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

revision = "d3a9f6b2c8e1"
down_revision = "c2d7e9f4a1b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("backend_node_id", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_sessions_backend_node_active",
        "sessions",
        ["backend_node_id", "ended_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_backend_node_active", table_name="sessions")
    op.drop_column("sessions", "backend_node_id")
