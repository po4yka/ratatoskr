"""add webwright_runs and user_browser_sessions

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None


webwright_run_status = postgresql.ENUM(
    "pending",
    "running",
    "completed",
    "error",
    "timeout",
    "cancelled",
    name="webwright_run_status",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    webwright_run_status.create(bind, checkfirst=True)

    op.create_table(
        "webwright_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column("allowed_domains_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            webwright_run_status,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("steps_used", sa.Integer(), nullable=True),
        sa.Column("llm_cost_usd", sa.Float(), nullable=True),
        sa.Column("final_answer", sa.Text(), nullable=True),
        sa.Column("trajectory_path", sa.String(length=500), nullable=True),
        sa.Column("screenshots_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["request_id"], ["requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webwright_runs_request_id", "webwright_runs", ["request_id"])
    op.create_index("ix_webwright_runs_user_id", "webwright_runs", ["user_id"])
    op.create_index("ix_webwright_runs_correlation_id", "webwright_runs", ["correlation_id"])

    op.create_table(
        "user_browser_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("encrypted_cookies", sa.LargeBinary(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "domain", name="uq_user_browser_sessions_user_domain"),
    )
    op.create_index("ix_user_browser_sessions_user_id", "user_browser_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_browser_sessions")
    op.drop_table("webwright_runs")

    bind = op.get_bind()
    webwright_run_status.drop(bind, checkfirst=True)
