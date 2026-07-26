"""add keyset indexes for bounded sync pagination

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-15

Sync v2 reads a small ordered head from each entity table using
``server_version > cursor`` and ``ORDER BY server_version, id``. These indexes
keep those reads proportional to the requested page instead of the user's full
history.
"""

from __future__ import annotations

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_index(
        "ix_requests_user_server_version_id",
        "requests",
        ["user_id", "server_version", "id"],
    )
    op.create_index(
        "ix_summaries_server_version_id",
        "summaries",
        ["server_version", "id"],
    )
    op.create_index(
        "ix_crawl_results_server_version_id",
        "crawl_results",
        ["server_version", "id"],
    )
    op.create_index(
        "ix_llm_calls_server_version_id",
        "llm_calls",
        ["server_version", "id"],
    )
    op.create_index(
        "ix_summary_highlights_user_server_version_id",
        "summary_highlights",
        ["user_id", "server_version", "id"],
    )
    op.create_index(
        "ix_tags_user_server_version_id",
        "tags",
        ["user_id", "server_version", "id"],
    )
    op.create_index(
        "ix_summary_tags_server_version_id",
        "summary_tags",
        ["server_version", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_summary_tags_server_version_id", table_name="summary_tags")
    op.drop_index("ix_tags_user_server_version_id", table_name="tags")
    op.drop_index(
        "ix_summary_highlights_user_server_version_id",
        table_name="summary_highlights",
    )
    op.drop_index("ix_llm_calls_server_version_id", table_name="llm_calls")
    op.drop_index("ix_crawl_results_server_version_id", table_name="crawl_results")
    op.drop_index("ix_summaries_server_version_id", table_name="summaries")
    op.drop_index("ix_requests_user_server_version_id", table_name="requests")
