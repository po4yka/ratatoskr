"""Replace ix_repositories_user_pushed_desc with a partial index.

``pushed_at`` is nullable on the ``repositories`` table.  Postgres orders
NULLs FIRST for DESC indexes by default, which means the full index scans
rows that will never satisfy the common query predicate
``WHERE user_id = $1 AND pushed_at IS NOT NULL ORDER BY pushed_at DESC``.

This migration replaces the existing index with a partial index that:

  * Excludes NULL ``pushed_at`` rows via ``WHERE pushed_at IS NOT NULL``.
  * Adds ``NULLS LAST`` to the DESC clause for explicitness (Postgres DESC
    defaults to NULLS FIRST; NULLS LAST ensures most-recently-pushed rows
    sort first within the index).

The replacement keeps the existing index name so query plans and docs stay stable.

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str = "0010"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_IDX = "ix_repositories_user_pushed_desc"


def upgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_IDX}"))

    op.execute(
        sa.text(
            f"""
            CREATE INDEX IF NOT EXISTS {_IDX}
                ON repositories (user_id, pushed_at DESC NULLS LAST)
                WHERE pushed_at IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_IDX}"))

    op.execute(
        sa.text(
            f"""
            CREATE INDEX IF NOT EXISTS {_IDX}
                ON repositories (user_id, pushed_at DESC)
            """
        )
    )
