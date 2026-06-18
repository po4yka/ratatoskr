"""Denormalize summary metadata columns (title, source_type, reading_time, topic_tags).

Audit findings 5B/5C/7A/7B: title, source_type, estimated_reading_time_min, and
topic_tags currently live only inside the json_payload JSONB blob on the
summaries table. That forces every query — including cheap list-view and
smart-collection scan queries — to either load the full blob or use slow
JSONB extraction expressions.

This migration adds nullable scalar columns that mirror those four payload
fields so queries can project/filter from indexed columns without loading
json_payload.  The existing json_payload column is unchanged; the new
columns are kept in sync by the write path from this point forward.

Columns added to ``summaries``
--------------------------------
- ``title``        TEXT        nullable — mirrors ``json_payload->>'title'``
- ``source_type``  TEXT        nullable — mirrors ``json_payload->>'source_type'``
- ``reading_time`` INTEGER     nullable — mirrors ``(json_payload->>'estimated_reading_time_min')::int``
- ``topic_tags``   JSONB       nullable — mirrors ``json_payload->'topic_tags'``

The upgrade backfills all existing rows in a single UPDATE (safe at this
scale; no chunk loop needed for the current dataset).  Non-numeric values in
``estimated_reading_time_min`` are silently coerced to NULL by the Postgres
``CASE / NULLIF / REGEXP`` guard.

Downgrade drops the four columns (index drops cascade automatically).

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str = "0029"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # 1. Add the four new nullable columns.
    op.add_column("summaries", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("summaries", sa.Column("source_type", sa.Text(), nullable=True))
    op.add_column("summaries", sa.Column("reading_time", sa.Integer(), nullable=True))
    op.add_column(
        "summaries",
        sa.Column("topic_tags", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    # 2. Backfill from existing json_payload rows.
    #    reading_time: guard against non-numeric strings with a CASE that tests
    #    the extracted text against a digits-only pattern before casting.
    op.execute(
        sa.text(
            """
            UPDATE summaries
            SET
                title       = json_payload->>'title',
                source_type = json_payload->>'source_type',
                reading_time = CASE
                    WHEN (json_payload->>'estimated_reading_time_min')
                         ~ '^-?[0-9]+$'
                    THEN (json_payload->>'estimated_reading_time_min')::integer
                    ELSE NULL
                END,
                topic_tags  = json_payload->'topic_tags'
            WHERE json_payload IS NOT NULL
            """
        )
    )

    op.create_index("ix_summaries_title", "summaries", ["title"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_summaries_title", table_name="summaries")
    op.drop_column("summaries", "topic_tags")
    op.drop_column("summaries", "reading_time")
    op.drop_column("summaries", "source_type")
    op.drop_column("summaries", "title")
