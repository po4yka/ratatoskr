"""add backup verification metadata

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("user_backups", sa.Column("checksum_sha256", sa.Text(), nullable=True))
    op.add_column(
        "user_backups",
        sa.Column(
            "item_counts_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("user_backups", sa.Column("schema_version", sa.Text(), nullable=True))
    op.add_column(
        "user_backups", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("user_backups", sa.Column("verification_status", sa.Text(), nullable=True))
    op.add_column("user_backups", sa.Column("verification_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_backups", "verification_error")
    op.drop_column("user_backups", "verification_status")
    op.drop_column("user_backups", "verified_at")
    op.drop_column("user_backups", "schema_version")
    op.drop_column("user_backups", "item_counts_json")
    op.drop_column("user_backups", "checksum_sha256")
