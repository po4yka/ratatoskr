"""Add public collection links.

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "collection_public_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token", sa.Text(), nullable=False, unique=True),
        sa.Column("collection_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_collection_public_links_token",
        "collection_public_links",
        ["token"],
        unique=True,
    )
    op.create_index(
        "ix_collection_public_links_collection_id",
        "collection_public_links",
        ["collection_id"],
    )
    op.create_index(
        "ix_collection_public_links_active",
        "collection_public_links",
        ["collection_id", "revoked_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_collection_public_links_active", table_name="collection_public_links")
    op.drop_index("ix_collection_public_links_collection_id", table_name="collection_public_links")
    op.drop_index("ix_collection_public_links_token", table_name="collection_public_links")
    op.drop_table("collection_public_links")
