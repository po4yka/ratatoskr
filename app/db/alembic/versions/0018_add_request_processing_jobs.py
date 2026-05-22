"""add durable request processing jobs

Revision ID: 0018
Revises: 0017_merge
Create Date: 2026-05-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017_merge"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "request_processing_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id"),
    )
    op.create_index(
        "ix_request_processing_jobs_status_retry",
        "request_processing_jobs",
        ["status", "retry_after"],
        unique=False,
    )
    op.create_index(
        "ix_request_processing_jobs_lease_expires_at",
        "request_processing_jobs",
        ["lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_request_processing_jobs_updated_at",
        "request_processing_jobs",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_request_processing_jobs_updated_at", table_name="request_processing_jobs")
    op.drop_index(
        "ix_request_processing_jobs_lease_expires_at", table_name="request_processing_jobs"
    )
    op.drop_index("ix_request_processing_jobs_status_retry", table_name="request_processing_jobs")
    op.drop_table("request_processing_jobs")
