"""add social fetch attempt operational columns

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "social_fetch_attempts", sa.Column("source_url", sa.String(length=2000), nullable=True)
    )
    op.add_column(
        "social_fetch_attempts", sa.Column("normalized_url", sa.String(length=2000), nullable=True)
    )
    op.add_column(
        "social_fetch_attempts",
        sa.Column("provider_resource_id", sa.String(length=255), nullable=True),
    )
    op.add_column("social_fetch_attempts", sa.Column("http_status", sa.Integer(), nullable=True))
    op.add_column(
        "social_fetch_attempts", sa.Column("auth_tier", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "social_fetch_attempts",
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "social_fetch_attempts", sa.Column("correlation_id", sa.String(length=128), nullable=True)
    )
    op.create_index(
        "ix_social_fetch_attempts_provider_resource",
        "social_fetch_attempts",
        ["provider", "provider_resource_id"],
    )
    op.create_index(
        "ix_social_fetch_attempts_normalized_url",
        "social_fetch_attempts",
        ["normalized_url"],
    )
    op.create_index(
        "ix_social_fetch_attempts_rate_limit_reset",
        "social_fetch_attempts",
        ["rate_limit_reset_at"],
    )
    op.create_index(
        "ix_social_fetch_attempts_correlation_id",
        "social_fetch_attempts",
        ["correlation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_social_fetch_attempts_correlation_id", table_name="social_fetch_attempts")
    op.drop_index("ix_social_fetch_attempts_rate_limit_reset", table_name="social_fetch_attempts")
    op.drop_index("ix_social_fetch_attempts_normalized_url", table_name="social_fetch_attempts")
    op.drop_index("ix_social_fetch_attempts_provider_resource", table_name="social_fetch_attempts")
    op.drop_column("social_fetch_attempts", "correlation_id")
    op.drop_column("social_fetch_attempts", "rate_limit_reset_at")
    op.drop_column("social_fetch_attempts", "auth_tier")
    op.drop_column("social_fetch_attempts", "http_status")
    op.drop_column("social_fetch_attempts", "provider_resource_id")
    op.drop_column("social_fetch_attempts", "normalized_url")
    op.drop_column("social_fetch_attempts", "source_url")
