"""Add outbound export integrations.

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_export_integrations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=True),
        sa.Column(
            "config_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_user_export_integrations_user_provider",
        "user_export_integrations",
        ["user_id", "provider"],
    )
    op.create_index(
        "ix_user_export_integrations_user_enabled",
        "user_export_integrations",
        ["user_id", "enabled"],
    )

    op.create_table(
        "export_delivery_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("integration_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("summary_id", sa.Integer(), nullable=True),
        sa.Column(
            "payload_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["user_export_integrations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_export_delivery_logs_integration_id",
        "export_delivery_logs",
        ["integration_id"],
    )
    op.create_index(
        "ix_export_delivery_logs_summary_id",
        "export_delivery_logs",
        ["summary_id"],
    )
    op.create_index(
        "ix_export_delivery_logs_created_at",
        "export_delivery_logs",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_export_delivery_logs_created_at", table_name="export_delivery_logs")
    op.drop_index("ix_export_delivery_logs_summary_id", table_name="export_delivery_logs")
    op.drop_index("ix_export_delivery_logs_integration_id", table_name="export_delivery_logs")
    op.drop_table("export_delivery_logs")
    op.drop_index(
        "ix_user_export_integrations_user_enabled",
        table_name="user_export_integrations",
    )
    op.drop_index(
        "ix_user_export_integrations_user_provider",
        table_name="user_export_integrations",
    )
    op.drop_table("user_export_integrations")
