"""add repository embedding index state

Revision ID: 0037
Revises: 0036
Create Date: 2026-06-18

Adds the same reconciliation cursor used by summary embeddings to repository
embeddings so Postgres can distinguish "embedding generated" from "Qdrant point
successfully indexed".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "repository_embeddings",
        sa.Column("content_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "repository_embeddings",
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "repository_embeddings",
        sa.Column(
            "index_status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_repository_embeddings_index_status",
        "repository_embeddings",
        ["index_status"],
    )
    op.create_index(
        "ix_repository_embeddings_last_indexed_at",
        "repository_embeddings",
        ["last_indexed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_repository_embeddings_last_indexed_at",
        table_name="repository_embeddings",
    )
    op.drop_index(
        "ix_repository_embeddings_index_status",
        table_name="repository_embeddings",
    )
    op.drop_column("repository_embeddings", "index_status")
    op.drop_column("repository_embeddings", "last_indexed_at")
    op.drop_column("repository_embeddings", "content_hash")
