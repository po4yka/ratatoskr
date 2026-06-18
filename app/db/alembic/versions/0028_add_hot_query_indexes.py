"""Add missing indexes for hot query paths.

Several frequently-filtered columns lacked indexes, forcing sequential scans at
scale. This migration adds them.

Indexes added
-------------
- ``correlation_id`` on ``requests``, ``crawl_results``,
  ``request_processing_jobs`` — the cross-cutting trace key (CLAUDE.md Operating
  Rule #1) was unindexed; trace lookups did sequential scans.
- ``audit_logs (ts)`` and ``audit_logs (event)`` — the table had no indexes;
  the admin dashboard filters by ``event`` and paginates by ``ts DESC``.
- ``refresh_tokens (family_id)`` — token-family revocation bulk-updates by
  family_id.
- ``summary_embeddings (index_status)`` — the reconciler counts pending rows by
  status.
- ``summaries (is_favorited) WHERE is_favorited = true`` — partial index for the
  favorites listing (favorited rows are a small subset).
- ``channels (last_fetched_at) WHERE is_active = true`` — partial index for the
  digest scheduler's "active channels by staleness" scan.

Deliberately NOT added: a standalone index on ``summaries.is_deleted`` or
``channels.is_active``. These are low-selectivity booleans (mostly one value), so
Postgres would not use a plain b-tree index on them; the access patterns that
filter them are already served by the partial indexes above and the existing
``ix_summaries_updated_at_where_not_deleted``.

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str = "0027"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# (index_name, CREATE statement body).
_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_requests_correlation_id", "ON requests (correlation_id)"),
    ("ix_crawl_results_correlation_id", "ON crawl_results (correlation_id)"),
    (
        "ix_request_processing_jobs_correlation_id",
        "ON request_processing_jobs (correlation_id)",
    ),
    ("ix_audit_logs_ts", "ON audit_logs (ts)"),
    ("ix_audit_logs_event", "ON audit_logs (event)"),
    ("ix_refresh_tokens_family_id", "ON refresh_tokens (family_id)"),
    ("ix_summary_embeddings_index_status", "ON summary_embeddings (index_status)"),
    (
        "ix_summaries_is_favorited",
        "ON summaries (is_favorited) WHERE is_favorited = true",
    ),
    (
        "ix_channels_active_last_fetched",
        "ON channels (last_fetched_at) WHERE is_active = true",
    ),
)


def upgrade() -> None:
    for name, body in _INDEXES:
        op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {name} {body}"))


def downgrade() -> None:
    for name, _body in reversed(_INDEXES):
        op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
