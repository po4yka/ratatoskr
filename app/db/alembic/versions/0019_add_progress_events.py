"""add durable progress events

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "progress_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
        sa.UniqueConstraint("request_id", "sequence", name="uq_progress_events_request_sequence"),
    )
    op.create_index(
        "ix_progress_events_request_sequence",
        "progress_events",
        ["request_id", "sequence"],
        unique=False,
    )
    op.create_index("ix_progress_events_event_id", "progress_events", ["event_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_progress_events_event_id", table_name="progress_events")
    op.drop_index("ix_progress_events_request_sequence", table_name="progress_events")
    op.drop_table("progress_events")
