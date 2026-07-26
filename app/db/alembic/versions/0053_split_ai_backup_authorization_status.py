"""Track AI backup authorization independently from backup outcomes.

Revision ID: 0053
Revises: 0052
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_authorization_status = postgresql.ENUM(
    "missing",
    "unverified",
    "valid",
    "expired",
    name="ai_backup_authorization_status",
    create_type=False,
)


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_type
                WHERE typname = 'ai_backup_authorization_status'
            ) THEN
                CREATE TYPE ai_backup_authorization_status AS ENUM
                    ('missing', 'unverified', 'valid', 'expired');
            END IF;
        END $$
    """)
    op.add_column(
        "ai_account_backups",
        sa.Column(
            "authorization_status",
            _authorization_status,
            server_default="missing",
            nullable=False,
        ),
    )
    op.add_column(
        "ai_account_backups",
        sa.Column("authorization_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("""
        UPDATE ai_account_backups
        SET authorization_status = CASE
                WHEN status = 'auth_expired' THEN 'expired'::ai_backup_authorization_status
                WHEN status = 'ok' THEN 'valid'::ai_backup_authorization_status
                ELSE 'unverified'::ai_backup_authorization_status
            END,
            authorization_checked_at = CASE
                WHEN status IN ('auth_expired', 'ok')
                    THEN COALESCE(last_attempt_at, last_backed_up_at, updated_at)
                ELSE NULL
            END
    """)
    op.execute("""
        UPDATE ai_account_backups
        SET status = CASE
            WHEN last_backed_up_at IS NULL THEN 'pending'::ai_backup_status
            ELSE 'ok'::ai_backup_status
        END
        WHERE status = 'auth_expired'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE ai_account_backups
        SET status = 'auth_expired'::ai_backup_status
        WHERE authorization_status = 'expired'
    """)
    op.drop_column("ai_account_backups", "authorization_checked_at")
    op.drop_column("ai_account_backups", "authorization_status")
    op.execute("DROP TYPE IF EXISTS ai_backup_authorization_status")
