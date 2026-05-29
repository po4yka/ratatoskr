"""Add use_http1_fallback column to git_mirrors.

Persists whether a mirror should start with HTTP/1.1 due to prior HTTP/2
protocol errors (e.g. "The server supports HTTP/2 but did not accept the
request").  When True, the sync task passes ``--no-v2`` (or equivalent) to
the git clone/fetch command for that mirror.

Changes
-------
1. Adds non-nullable ``use_http1_fallback BOOLEAN NOT NULL DEFAULT false`` to
   ``git_mirrors`` with a server-side default so existing rows backfill
   without a table rewrite.

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str = "0033"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "git_mirrors",
        sa.Column(
            "use_http1_fallback",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("git_mirrors", "use_http1_fallback")
