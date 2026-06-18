"""Add summary_embeddings drift-tracking columns.

Adds ``content_hash``, ``last_indexed_at``, and ``index_status`` to
``summary_embeddings`` so the vector reconciler can detect drift
(``last_indexed_at < summaries.updated_at``) and the embedding generator can
short-circuit unchanged content.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: tuple[str, str] = ("0006", "0001b")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add index tracking columns to summary_embeddings
    op.add_column(
        "summary_embeddings",
        sa.Column("content_hash", sa.Text(), nullable=True),
    )
    op.add_column(
        "summary_embeddings",
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "summary_embeddings",
        sa.Column(
            "index_status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_index(
        "ix_summary_embeddings_last_indexed_at",
        "summary_embeddings",
        ["last_indexed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_summary_embeddings_last_indexed_at", table_name="summary_embeddings")
    op.drop_column("summary_embeddings", "index_status")
    op.drop_column("summary_embeddings", "last_indexed_at")
    op.drop_column("summary_embeddings", "content_hash")
