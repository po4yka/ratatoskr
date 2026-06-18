"""Add Taskiq failed-job dead-letter table.

Revision ID: 0039
Revises: 0038
Create Date: 2026-06-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import JSONB

# revision identifiers, used by Alembic.
revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "taskiq_failed_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("args_json", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("kwargs_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("labels_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("traceback_text", sa.Text(), nullable=False),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="dead_letter"),
        sa.Column(
            "last_failed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("requeued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_taskiq_failed_jobs_task_name_last_failed_at",
        "taskiq_failed_jobs",
        ["task_name", "last_failed_at"],
    )
    op.create_index(
        "ix_taskiq_failed_jobs_status_last_failed_at",
        "taskiq_failed_jobs",
        ["status", "last_failed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_taskiq_failed_jobs_status_last_failed_at", table_name="taskiq_failed_jobs")
    op.drop_index("ix_taskiq_failed_jobs_task_name_last_failed_at", table_name="taskiq_failed_jobs")
    op.drop_table("taskiq_failed_jobs")
