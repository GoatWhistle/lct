"""student-authored scenario proposals with instructor moderation

Revision ID: a8b5c2d9e1f4
Revises: f7a3c9d1e2b4
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

revision = "a8b5c2d9e1f4"
down_revision = "f7a3c9d1e2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scenario_submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("author_trainee_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("incident_type", sa.String(length=20), nullable=False),
        sa.Column("level", sa.String(length=4), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("address", sa.String(length=500), server_default="", nullable=False),
        sa.Column("victims", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("review_comment", sa.Text(), server_default="", nullable=False),
        sa.Column("reviewed_by", sa.String(length=80), nullable=True),
        sa.Column("scenario_id", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["author_trainee_id"], ["trainees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scenario_submissions_group_status",
        "scenario_submissions",
        ["group_id", "status", "created_at"],
    )
    op.create_index(
        "ix_scenario_submissions_author",
        "scenario_submissions",
        ["author_trainee_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scenario_submissions_author", table_name="scenario_submissions")
    op.drop_index("ix_scenario_submissions_group_status", table_name="scenario_submissions")
    op.drop_table("scenario_submissions")
