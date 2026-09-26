"""store the submitted KIO snapshot for instructor review

Revision ID: b9c6d3e2f1a0
Revises: a8b5c2d9e1f4
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b9c6d3e2f1a0"
down_revision = "a8b5c2d9e1f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scenario_submissions",
        sa.Column("kio", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scenario_submissions", "kio")
