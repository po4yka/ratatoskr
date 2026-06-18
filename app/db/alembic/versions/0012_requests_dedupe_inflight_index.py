"""Add composite index for in-flight dedupe queries on requests.

``RequestRepositoryAdapter.async_find_recent_request_by_dedupe`` issues:

    SELECT * FROM requests
    WHERE dedupe_hash = :hash
      AND status IN ('processing', 'pending', 'error')
      AND updated_at >= :cutoff
    ORDER BY updated_at DESC LIMIT 1

The table already has a UNIQUE constraint on ``dedupe_hash``, so the
equality predicate is fast in isolation.  However, at scale the planner
may still fetch the row to evaluate ``status`` and ``updated_at``
filters.  A covering composite index on ``(dedupe_hash, status,
updated_at DESC)`` allows Postgres to answer the full query from the
index alone without a heap fetch.

The index covers the full in-flight dedupe predicate used by the request repository.

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str = "0011"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_IDX = "ix_requests_dedupe_status_updated"


def upgrade() -> None:
    op.execute(
        sa.text(
            f"""
            CREATE INDEX IF NOT EXISTS {_IDX}
                ON requests (dedupe_hash, status, updated_at DESC)
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_IDX}"))
