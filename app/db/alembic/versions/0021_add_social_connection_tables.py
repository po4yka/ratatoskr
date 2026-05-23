"""add social connection tables

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | None = None
depends_on: str | None = None


social_provider = postgresql.ENUM(
    "x", "instagram", "threads", name="social_provider", create_type=False
)
social_auth_type = postgresql.ENUM(
    "oauth2", "cookie", "manual", name="social_auth_type", create_type=False
)
social_connection_status = postgresql.ENUM(
    "active",
    "needs_reauth",
    "revoked",
    "disabled",
    name="social_connection_status",
    create_type=False,
)
social_auth_state_status = postgresql.ENUM(
    "pending", "consumed", "expired", name="social_auth_state_status", create_type=False
)
social_fetch_attempt_status = postgresql.ENUM(
    "started", "succeeded", "failed", name="social_fetch_attempt_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    social_provider.create(bind, checkfirst=True)
    social_auth_type.create(bind, checkfirst=True)
    social_connection_status.create(bind, checkfirst=True)
    social_auth_state_status.create(bind, checkfirst=True)
    social_fetch_attempt_status.create(bind, checkfirst=True)

    op.create_table(
        "social_connections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", social_provider, nullable=False),
        sa.Column("auth_type", social_auth_type, nullable=False),
        sa.Column("provider_user_id", sa.String(length=255), nullable=True),
        sa.Column("provider_username", sa.String(length=255), nullable=True),
        sa.Column("encrypted_access_token", sa.LargeBinary(), nullable=True),
        sa.Column("encrypted_refresh_token", sa.LargeBinary(), nullable=True),
        sa.Column("token_scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            social_connection_status,
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "provider", name="uq_social_connections_user_provider"),
    )
    op.create_index("ix_social_connections_user_id", "social_connections", ["user_id"])
    op.create_index(
        "ix_social_connections_user_status", "social_connections", ["user_id", "status"]
    )
    op.create_index(
        "ix_social_connections_provider_user",
        "social_connections",
        ["provider", "provider_user_id"],
    )

    op.create_table(
        "social_auth_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", social_provider, nullable=False),
        sa.Column("state_hash", sa.String(length=128), nullable=False),
        sa.Column("encrypted_code_verifier", sa.LargeBinary(), nullable=True),
        sa.Column("redirect_uri", sa.String(length=1000), nullable=True),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            social_auth_state_status,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "state_hash", name="uq_social_auth_states_provider_state"),
    )
    op.create_index("ix_social_auth_states_user_id", "social_auth_states", ["user_id"])
    op.create_index(
        "ix_social_auth_states_user_provider", "social_auth_states", ["user_id", "provider"]
    )
    op.create_index("ix_social_auth_states_expires_at", "social_auth_states", ["expires_at"])

    op.create_table(
        "social_fetch_attempts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", social_provider, nullable=False),
        sa.Column("attempt_type", sa.String(length=100), nullable=False),
        sa.Column(
            "status",
            social_fetch_attempt_status,
            server_default=sa.text("'started'"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["social_connections.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_social_fetch_attempts_connection_id", "social_fetch_attempts", ["connection_id"]
    )
    op.create_index("ix_social_fetch_attempts_user_id", "social_fetch_attempts", ["user_id"])
    op.create_index(
        "ix_social_fetch_attempts_connection_started",
        "social_fetch_attempts",
        ["connection_id", "started_at"],
    )
    op.create_index(
        "ix_social_fetch_attempts_user_provider",
        "social_fetch_attempts",
        ["user_id", "provider"],
    )


def downgrade() -> None:
    op.drop_table("social_fetch_attempts")
    op.drop_table("social_auth_states")
    op.drop_table("social_connections")

    bind = op.get_bind()
    social_fetch_attempt_status.drop(bind, checkfirst=True)
    social_auth_state_status.drop(bind, checkfirst=True)
    social_connection_status.drop(bind, checkfirst=True)
    social_auth_type.drop(bind, checkfirst=True)
    social_provider.drop(bind, checkfirst=True)
