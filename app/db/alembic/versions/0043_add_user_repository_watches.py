"""Add user repository watches.

Revision ID: 0043
Revises: 0042
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_repository_watches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("watch_readme", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("watch_releases", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_readme_sha256", sa.String(length=64), nullable=True),
        sa.Column("last_notified_readme_sha256", sa.String(length=64), nullable=True),
        sa.Column("last_release_tag", sa.String(length=255), nullable=True),
        sa.Column("last_notified_release_tag", sa.String(length=255), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id", "repository_id", name="uq_user_repository_watches_user_repo"
        ),
    )
    op.create_index(
        "ix_user_repository_watches_repository_id",
        "user_repository_watches",
        ["repository_id"],
    )
    op.create_index(
        "ix_user_repository_watches_user_created",
        "user_repository_watches",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_repository_watches_user_created", table_name="user_repository_watches")
    op.drop_index("ix_user_repository_watches_repository_id", table_name="user_repository_watches")
    op.drop_table("user_repository_watches")
