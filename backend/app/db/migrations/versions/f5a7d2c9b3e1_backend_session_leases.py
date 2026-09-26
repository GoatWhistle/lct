"""add expiring backend ownership leases and fencing epochs

Revision ID: f5a7d2c9b3e1
Revises: e4b7c1d2a9f0
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op

revision = "f5a7d2c9b3e1"
down_revision = "e4b7c1d2a9f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column(
            "backend_fencing_epoch",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "sessions",
        sa.Column("backend_lease_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sessions", "backend_lease_until")
    op.drop_column("sessions", "backend_fencing_epoch")
