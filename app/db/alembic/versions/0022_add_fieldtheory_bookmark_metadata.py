"""add fieldtheory_bookmark_metadata

Sidecar table for ``requests`` rows ingested via the fieldtheory CLI sync path.
Schema source of truth: ``docs/explanation/fieldtheory-integration.md`` lines 60-77.

Notes:
- ``SourceKind.FIELDTHEORY_BOOKMARK`` and ``RequestStatus.FIELDTHEORY_IMPORTED``
  are Python ``StrEnum`` values mapped to ``TEXT`` columns (``requests.type``,
  ``requests.status``, ``sources.kind``). There is no Postgres native enum to
  ``ALTER TYPE ... ADD VALUE`` for these, so this migration does not touch any
  type definitions.
- ``tweet_text_tsv`` is a ``GENERATED ALWAYS AS ... STORED`` column populated
  from ``tweet_text``; the MCP ``fieldtheory_search`` tool queries it via
  ``ts_rank_cd``.
- ``CheckConstraint`` pins the v2 category vocabulary. Adding a new category
  requires both a coordinated ``ft`` change and a follow-up migration that
  drops + recreates the constraint.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | None = None
depends_on: str | None = None


_FIELDTHEORY_CATEGORY_VALUES: tuple[str, ...] = (
    "tool",
    "security",
    "technique",
    "launch",
    "research",
    "opinion",
    "commerce",
)


def upgrade() -> None:
    op.create_table(
        "fieldtheory_bookmark_metadata",
        sa.Column("request_id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("fieldtheory_id", sa.Text(), nullable=False),
        sa.Column("fieldtheory_category", sa.Text(), nullable=False),
        sa.Column("tweet_text", sa.Text(), nullable=True),
        sa.Column(
            "tweet_text_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(tweet_text, ''))",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("tweet_author", sa.Text(), nullable=True),
        sa.Column("tweet_url", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("request_id"),
        sa.CheckConstraint(
            "fieldtheory_category IN ("
            + ", ".join(f"'{value}'" for value in _FIELDTHEORY_CATEGORY_VALUES)
            + ")",
            name="ck_fieldtheory_bookmark_metadata_category",
        ),
    )
    op.create_index(
        "ix_fieldtheory_bookmark_metadata_fieldtheory_id",
        "fieldtheory_bookmark_metadata",
        ["fieldtheory_id"],
        unique=True,
    )
    op.create_index(
        "ix_fieldtheory_bookmark_metadata_category",
        "fieldtheory_bookmark_metadata",
        ["fieldtheory_category"],
        unique=False,
    )
    op.create_index(
        "ix_fieldtheory_bookmark_metadata_tweet_text_tsv",
        "fieldtheory_bookmark_metadata",
        ["tweet_text_tsv"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_fieldtheory_bookmark_metadata_tweet_text_tsv",
        table_name="fieldtheory_bookmark_metadata",
        postgresql_using="gin",
    )
    op.drop_index(
        "ix_fieldtheory_bookmark_metadata_category",
        table_name="fieldtheory_bookmark_metadata",
    )
    op.drop_index(
        "ix_fieldtheory_bookmark_metadata_fieldtheory_id",
        table_name="fieldtheory_bookmark_metadata",
    )
    op.drop_table("fieldtheory_bookmark_metadata")
