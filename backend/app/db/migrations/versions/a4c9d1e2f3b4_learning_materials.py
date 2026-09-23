"""учебные материалы и назначения

Revision ID: a4c9d1e2f3b4
Revises: e6e2ab171dd7
Create Date: 2026-09-23
"""

from alembic import op
import sqlalchemy as sa


revision = "a4c9d1e2f3b4"
down_revision = "e6e2ab171dd7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learning_materials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("level", sa.String(length=4), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("scenario_id", sa.String(length=80), nullable=True),
        sa.Column("file_name", sa.String(length=240), nullable=True),
        sa.Column("media_type", sa.String(length=120), nullable=True),
        sa.Column("file_data", sa.LargeBinary(), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "material_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("trainee_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_by", sa.String(length=80), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["material_id"], ["learning_materials.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trainee_id"], ["trainees.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("material_id", "trainee_id", name="uq_material_assignment"),
    )
    op.create_index(
        "ix_material_assignments_trainee",
        "material_assignments",
        ["trainee_id", "assigned_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_material_assignments_trainee", table_name="material_assignments")
    op.drop_table("material_assignments")
    op.drop_table("learning_materials")
