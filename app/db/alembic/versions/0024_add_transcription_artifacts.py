"""add transcription jobs and artifacts

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "transcription_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("backend", sa.String(length=100), nullable=True),
        sa.Column("tokens_mode", sa.String(length=100), nullable=True),
        sa.Column("model_identifier", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("audio_hash", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transcription_jobs_user_id", "transcription_jobs", ["user_id"])
    op.create_index(
        "ix_transcription_jobs_user_status", "transcription_jobs", ["user_id", "status"]
    )
    op.create_index("ix_transcription_jobs_request_id", "transcription_jobs", ["request_id"])
    op.create_index(
        "ix_transcription_jobs_telegram_message",
        "transcription_jobs",
        ["telegram_chat_id", "telegram_message_id"],
    )
    op.create_index("ix_transcription_jobs_audio_hash", "transcription_jobs", ["audio_hash"])
    op.create_index(
        "ix_transcription_jobs_correlation_id", "transcription_jobs", ["correlation_id"]
    )

    op.create_table(
        "transcription_artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("backend", sa.String(length=100), nullable=True),
        sa.Column("tokens_mode", sa.String(length=100), nullable=True),
        sa.Column("model_identifier", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("plain_text", sa.Text(), nullable=False),
        sa.Column("sentences_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("speaker_turns_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("audio_hash", sa.String(length=128), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["transcription_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transcription_artifacts_job_id", "transcription_artifacts", ["job_id"])
    op.create_index("ix_transcription_artifacts_user_id", "transcription_artifacts", ["user_id"])
    op.create_index(
        "ix_transcription_artifacts_user_created",
        "transcription_artifacts",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_transcription_artifacts_request_id", "transcription_artifacts", ["request_id"]
    )
    op.create_index(
        "ix_transcription_artifacts_audio_hash", "transcription_artifacts", ["audio_hash"]
    )
    op.create_index(
        "ix_transcription_artifacts_correlation_id",
        "transcription_artifacts",
        ["correlation_id"],
    )


def downgrade() -> None:
    op.drop_table("transcription_artifacts")
    op.drop_table("transcription_jobs")
