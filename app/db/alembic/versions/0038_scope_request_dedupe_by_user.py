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
    # Restoring the pre-0038 GLOBAL unique constraints is only possible when no
    # cross-user duplicate dedupe_hash / paper_canonical_id rows exist. But the
    # forward migration exists precisely to allow those: with more than one
    # ALLOWED_USER_IDS, two users may legitimately submit the same URL/paper, and
    # request_repository.py's ON CONFLICT (user_id, dedupe_hash) writes depend on
    # it. So check first and refuse with a clear message rather than aborting
    # part-way on a raw Postgres unique-violation. Alembic wraps the downgrade in
    # a transaction, so raising here leaves the schema untouched.
    bind = op.get_bind()
    duplicate_groups = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM ("
            "  SELECT 1 FROM requests WHERE dedupe_hash IS NOT NULL"
            "  GROUP BY dedupe_hash HAVING COUNT(*) > 1"
            "  UNION ALL"
            "  SELECT 1 FROM requests WHERE paper_canonical_id IS NOT NULL"
            "  GROUP BY paper_canonical_id HAVING COUNT(*) > 1"
            ") AS dupes"
        )
    ).scalar()
    if duplicate_groups:
        msg = (
            f"Cannot downgrade past 0038: {duplicate_groups} dedupe_hash/"
            "paper_canonical_id value(s) are duplicated across users, which the "
            "global UNIQUE constraints this downgrade restores would reject. "
            "De-duplicate the requests table (keep one row per value) first, or "
            "treat this revision as one-way."
        )
        raise RuntimeError(msg)

    op.drop_index("ux_requests_user_paper_canonical_id", table_name="requests")
    op.drop_index("ux_requests_user_dedupe_hash", table_name="requests")
    op.create_unique_constraint(
        "uq_requests_paper_canonical_id",
        "requests",
        ["paper_canonical_id"],
    )
    op.create_unique_constraint("requests_dedupe_hash_key", "requests", ["dedupe_hash"])
