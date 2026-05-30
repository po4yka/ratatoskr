"""Add lifetime failure-tracking columns to git_mirrors.

Adds two columns that record cumulative failure statistics for a mirror,
separate from the existing ``consecutive_failures`` counter which resets on
success:

* ``total_failures`` -- monotonically increasing count of every failed sync
  attempt over the lifetime of the mirror; never decremented or reset.
* ``last_failure_at`` -- timestamp of the most recent failure, distinct from
  ``last_attempt_at`` which advances on both success and failure.

Changes
-------
1. Adds non-nullable ``total_failures INTEGER NOT NULL DEFAULT 0`` with a
   server-side default so existing rows backfill without a table rewrite.
2. Adds nullable ``last_failure_at TIMESTAMP WITH TIME ZONE``.

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str = "0034"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "git_mirrors",
        sa.Column(
            "total_failures",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "git_mirrors",
        sa.Column(
            "last_failure_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("git_mirrors", "last_failure_at")
    op.drop_column("git_mirrors", "total_failures")
