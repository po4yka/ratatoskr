"""Add email delivery sink tables.

Revision ID: 0040
Revises: 0039
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.types import JSONB

# revision identifiers, used by Alembic.
revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_email_addresses",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("email_canonical", sa.Text(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_token_hash", sa.Text(), nullable=True),
        sa.Column("confirmation_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_user_email_addresses_user_id",
        "user_email_addresses",
        ["user_id"],
    )
    op.create_index(
        "ux_user_email_addresses_user_email_canonical",
        "user_email_addresses",
        ["user_id", "email_canonical"],
        unique=True,
    )
    op.create_index(
        "ix_user_email_addresses_confirmation_token_hash",
        "user_email_addresses",
        ["confirmation_token_hash"],
    )
    op.create_table(
        "email_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("email_address_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata_json", JSONB(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["email_address_id"], ["user_email_addresses.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_email_deliveries_user_id_created_at",
        "email_deliveries",
        ["user_id", "created_at"],
    )
    op.create_index("ix_email_deliveries_correlation_id", "email_deliveries", ["correlation_id"])
    op.create_index("ix_email_deliveries_status", "email_deliveries", ["status"])
    op.add_column(
        "user_digest_preferences",
        sa.Column("delivery_channel", sa.Text(), nullable=False, server_default="telegram"),
    )
    op.add_column(
        "user_digest_preferences",
        sa.Column("email_address_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_digest_preferences_email_address_id",
        "user_digest_preferences",
        "user_email_addresses",
        ["email_address_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_user_digest_preferences_email_address_id",
        "user_digest_preferences",
        type_="foreignkey",
    )
    op.drop_column("user_digest_preferences", "email_address_id")
    op.drop_column("user_digest_preferences", "delivery_channel")
    op.drop_index("ix_email_deliveries_status", table_name="email_deliveries")
    op.drop_index("ix_email_deliveries_correlation_id", table_name="email_deliveries")
    op.drop_index("ix_email_deliveries_user_id_created_at", table_name="email_deliveries")
    op.drop_table("email_deliveries")
    op.drop_index(
        "ix_user_email_addresses_confirmation_token_hash",
        table_name="user_email_addresses",
    )
    op.drop_index(
        "ux_user_email_addresses_user_email_canonical",
        table_name="user_email_addresses",
    )
    op.drop_index("ix_user_email_addresses_user_id", table_name="user_email_addresses")
    op.drop_table("user_email_addresses")
