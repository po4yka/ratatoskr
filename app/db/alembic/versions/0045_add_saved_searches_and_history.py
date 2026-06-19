"""Add saved searches and opt-in search history.

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "filters_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_saved_searches_user_created",
        "saved_searches",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_saved_searches_user_name",
        "saved_searches",
        ["user_id", "name"],
    )

    op.create_table(
        "search_history",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column(
            "filters_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_search_history_user_created",
        "search_history",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_search_history_user_created", table_name="search_history")
    op.drop_table("search_history")
    op.drop_index("ix_saved_searches_user_name", table_name="saved_searches")
    op.drop_index("ix_saved_searches_user_created", table_name="saved_searches")
    op.drop_table("saved_searches")
