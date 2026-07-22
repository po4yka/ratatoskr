"""add service_credentials for UI-managed provider secrets

Revision ID: 0054
Revises: 0053
Create Date: 2026-07-21

Backs the settings UI that lets the owner install runtime service credentials
(LLM provider keys, scraper tokens, ...) without editing ``.env`` and
redeploying. Values are Fernet-encrypted with the same key material as every
other integration secret, so this table never holds plaintext.

Only keys listed in ``app/config/credential_catalog.py`` are storable.
Key-encryption keys and bootstrap secrets are excluded there by design and stay
in the environment.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "service_credentials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("credential_key", sa.String(length=128), nullable=False),
        sa.Column("encrypted_value", sa.LargeBinary(), nullable=False),
        sa.Column("hint", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "credential_key", name="uq_service_credentials_user_credential"
        ),
    )
    op.create_index(
        "ix_service_credentials_user_id", "service_credentials", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_service_credentials_user_id", table_name="service_credentials")
    op.drop_table("service_credentials")
