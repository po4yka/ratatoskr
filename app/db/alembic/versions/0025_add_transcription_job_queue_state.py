"""add transcription job queue state

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("transcription_jobs", sa.Column("source_url", sa.Text(), nullable=True))
    op.add_column(
        "transcription_jobs", sa.Column("idempotency_key", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "transcription_jobs", sa.Column("current_stage", sa.String(length=100), nullable=True)
    )
    op.add_column("transcription_jobs", sa.Column("progress", sa.Float(), nullable=True))
    op.add_column(
        "transcription_jobs",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "transcription_jobs",
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
    )
    op.add_column(
        "transcription_jobs", sa.Column("lease_owner", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "transcription_jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "transcription_jobs", sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "transcription_jobs", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "transcription_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "transcription_jobs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_transcription_jobs_idempotency_key",
        "transcription_jobs",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_transcription_jobs_status_retry", "transcription_jobs", ["status", "retry_after"]
    )
    op.create_index(
        "ix_transcription_jobs_lease_expires_at", "transcription_jobs", ["lease_expires_at"]
    )
    op.create_table(
        "transcription_progress_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["transcription_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_transcription_progress_events_job_sequence",
        "transcription_progress_events",
        ["job_id", "sequence"],
        unique=True,
    )
    op.create_index(
        "ix_transcription_progress_events_event_id",
        "transcription_progress_events",
        ["event_id"],
        unique=True,
    )
    op.create_index(
        "ix_transcription_progress_events_correlation_id",
        "transcription_progress_events",
        ["correlation_id"],
    )


def downgrade() -> None:
    op.drop_table("transcription_progress_events")
    op.drop_index("ix_transcription_jobs_lease_expires_at", table_name="transcription_jobs")
    op.drop_index("ix_transcription_jobs_status_retry", table_name="transcription_jobs")
    op.drop_index("ix_transcription_jobs_idempotency_key", table_name="transcription_jobs")
    for column in (
        "completed_at",
        "started_at",
        "queued_at",
        "retry_after",
        "lease_expires_at",
        "lease_owner",
        "max_attempts",
        "attempt_count",
        "progress",
        "current_stage",
        "idempotency_key",
        "source_url",
    ):
        op.drop_column("transcription_jobs", column)
