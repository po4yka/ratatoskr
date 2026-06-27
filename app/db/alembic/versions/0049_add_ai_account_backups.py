"""Add ai_account_backups table for AI account backup state.

Creates the ``ai_account_backups`` table and two Postgres enum types for the
ChatGPT/Claude account-backup subsystem:

  ai_backup_service  ('chatgpt', 'claude')
  ai_backup_status   ('pending', 'ok', 'failed', 'auth_expired', 'disabled')

One row per (user_id, service). The authenticated session blob is stored
elsewhere (user_browser_sessions); this table holds backup lifecycle state.

Indexes
-------
- ``ix_ai_account_backups_user_id``     -- on user_id (implicit FK lookup)
- ``ix_ai_account_backups_user_status`` -- composite (user_id, status)

Unique constraint
-----------------
- ``uq_ai_account_backups_user_service`` -- (user_id, service)

Revision ID: 0049
Revises: 0048
Create Date: 2026-06-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Dialect-level enum references with create_type=False so SQLAlchemy never
# auto-emits CREATE TYPE inside op.create_table. The CREATE TYPE is emitted
# explicitly via DO blocks in upgrade().
_ai_backup_service = postgresql.ENUM(
    "chatgpt", "claude", name="ai_backup_service", create_type=False
)
_ai_backup_status = postgresql.ENUM(
    "pending",
    "ok",
    "failed",
    "auth_expired",
    "disabled",
    name="ai_backup_status",
    create_type=False,
)


def upgrade() -> None:
    # 1. Create the two Postgres enum types via DO blocks (idempotent).
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ai_backup_service') THEN
                CREATE TYPE ai_backup_service AS ENUM ('chatgpt', 'claude');
            END IF;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ai_backup_status') THEN
                CREATE TYPE ai_backup_status AS ENUM
                    ('pending', 'ok', 'failed', 'auth_expired', 'disabled');
            END IF;
        END $$
    """)

    # 2. Create the ai_account_backups table.
    op.create_table(
        "ai_account_backups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("service", _ai_backup_service, nullable=False),
        sa.Column(
            "status",
            _ai_backup_status,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("last_backed_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_category", sa.String(length=50), nullable=True),
        sa.Column("counts_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("last_backup_path", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.telegram_user_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "service", name="uq_ai_account_backups_user_service"),
    )
    op.create_index(
        "ix_ai_account_backups_user_id", "ai_account_backups", ["user_id"], unique=False
    )
    op.create_index(
        "ix_ai_account_backups_user_status",
        "ai_account_backups",
        ["user_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_account_backups_user_status", table_name="ai_account_backups")
    op.drop_index("ix_ai_account_backups_user_id", table_name="ai_account_backups")
    op.drop_table("ai_account_backups")
    op.execute("DROP TYPE IF EXISTS ai_backup_status")
    op.execute("DROP TYPE IF EXISTS ai_backup_service")
