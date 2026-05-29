"""Add readme_content_hash and readme_indexed_at columns to git_mirrors.

Supports semantic indexing of README content from non-GitHub (arbitrary-URL)
git mirrors into Qdrant for semantic search. Only mirrors with
repository_id IS NULL (manual/arbitrary targets) are indexed via this path;
GitHub-linked mirrors are already searchable via the repository-embedding path.

Changes
-------
1. Adds nullable ``readme_content_hash VARCHAR(64)`` to ``git_mirrors``
   (SHA-256 hex digest of the indexed README text; used for content-hash dedup).
2. Adds nullable ``readme_indexed_at TIMESTAMPTZ`` to ``git_mirrors``
   (timestamp of the last successful Qdrant upsert for this mirror's README).

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str = "0032"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "git_mirrors",
        sa.Column("readme_content_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "git_mirrors",
        sa.Column("readme_indexed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("git_mirrors", "readme_indexed_at")
    op.drop_column("git_mirrors", "readme_content_hash")
