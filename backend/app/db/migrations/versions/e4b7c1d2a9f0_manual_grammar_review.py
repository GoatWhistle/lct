"""require a grammar check after manual scenario edits

Revision ID: e4b7c1d2a9f0
Revises: d3a9f6b2c8e1
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op

revision = "e4b7c1d2a9f0"
down_revision = "d3a9f6b2c8e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenarios",
        sa.Column(
            "manual_edit_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "scenarios",
        sa.Column("grammar_check_hash", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scenarios", "grammar_check_hash")
    op.drop_column("scenarios", "manual_edit_pending")
