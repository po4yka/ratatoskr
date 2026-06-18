"""Scope request dedupe keys by user.

Revision ID: 0038
Revises: 0037
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE requests DROP CONSTRAINT IF EXISTS requests_dedupe_hash_key"))
    op.execute(
        sa.text("ALTER TABLE requests DROP CONSTRAINT IF EXISTS uq_requests_paper_canonical_id")
    )
    op.execute(
        sa.text("ALTER TABLE requests DROP CONSTRAINT IF EXISTS requests_paper_canonical_id_key")
    )
    op.create_index(
        "ux_requests_user_dedupe_hash",
        "requests",
        ["user_id", "dedupe_hash"],
        unique=True,
        postgresql_where=sa.text("dedupe_hash IS NOT NULL"),
    )
    op.create_index(
        "ux_requests_user_paper_canonical_id",
        "requests",
        ["user_id", "paper_canonical_id"],
        unique=True,
        postgresql_where=sa.text("paper_canonical_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_requests_user_paper_canonical_id", table_name="requests")
    op.drop_index("ux_requests_user_dedupe_hash", table_name="requests")
    op.create_unique_constraint(
        "uq_requests_paper_canonical_id",
        "requests",
        ["paper_canonical_id"],
    )
    op.create_unique_constraint("requests_dedupe_hash_key", "requests", ["dedupe_hash"])
