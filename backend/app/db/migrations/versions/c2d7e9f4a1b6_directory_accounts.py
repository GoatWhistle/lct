"""provision and identify local AD/LDAP accounts

Revision ID: c2d7e9f4a1b6
Revises: b9c6d3e2f1a0
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op

revision = "c2d7e9f4a1b6"
down_revision = "b9c6d3e2f1a0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "auth_provider",
            sa.String(length=16),
            server_default="local",
            nullable=False,
        ),
    )
    op.add_column(
        "users", sa.Column("directory_subject", sa.String(length=256), nullable=True)
    )
    op.create_unique_constraint(
        "uq_users_directory_subject", "users", ["directory_subject"]
    )
    op.create_check_constraint(
        "ck_users_auth_provider", "users", "auth_provider IN ('local', 'ldap')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_auth_provider", "users", type_="check")
    op.drop_constraint("uq_users_directory_subject", "users", type_="unique")
    op.drop_column("users", "directory_subject")
    op.drop_column("users", "auth_provider")
