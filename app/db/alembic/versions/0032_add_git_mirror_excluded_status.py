"""Add 'excluded' to git_mirror_status enum and excluded_at column.

Tombstones permanently-gone mirror targets (repository deleted or renamed
upstream, resulting in a 404/410/"repository not found" from git) so they are
excluded from future sync runs without cycling through the FAILED cooldown.

Changes
-------
1. Adds value ``'excluded'`` to the ``git_mirror_status`` Postgres enum.
2. Adds nullable ``excluded_at TIMESTAMPTZ`` column to ``git_mirrors``.

Downgrade note
--------------
Postgres does not support removing values from an enum without recreating the
type and rewriting every referencing column -- a heavy migration that should
ship as a standalone operation if ever needed.  Downgrade here drops only the
``excluded_at`` column and leaves the ``'excluded'`` enum value in place (the
same intentional no-op pattern used in revision 0027 for
``llm_attempt_trigger``).

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str = "0031"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # 1. Add 'excluded' to the git_mirror_status enum.
    #    ALTER TYPE ... ADD VALUE is transaction-unsafe in older Postgres, but
    #    supported as-is in PG13+.  Ratatoskr targets PG16, so this is fine
    #    (same pattern as revision 0027 for llm_attempt_trigger).
    op.execute("ALTER TYPE git_mirror_status ADD VALUE IF NOT EXISTS 'excluded'")

    # 2. Add the excluded_at column (nullable timestamptz).
    op.add_column(
        "git_mirrors",
        sa.Column("excluded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Drop the column only.  The 'excluded' enum value is intentionally left in
    # place -- removing enum values in Postgres requires recreating the type and
    # rewriting every referencing column, which is a separate heavy migration.
    op.drop_column("git_mirrors", "excluded_at")
