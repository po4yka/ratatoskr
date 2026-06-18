"""Scope request dedupe keys by user.

Revision ID: 0038_scope_request_dedupe_by_user
Revises: 0037_repository_embedding_index_state
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0038_scope_request_dedupe_by_user"
down_revision = "0037_repository_embedding_index_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("requests_dedupe_hash_key", "requests", type_="unique")
    op.drop_constraint("requests_paper_canonical_id_key", "requests", type_="unique")
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
        "requests_paper_canonical_id_key",
        "requests",
        ["paper_canonical_id"],
    )
    op.create_unique_constraint("requests_dedupe_hash_key", "requests", ["dedupe_hash"])
