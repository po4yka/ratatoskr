"""Add typed user profile fields.

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("onboarding_completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("locale", sa.Text(), nullable=False, server_default="en"))
    op.add_column("users", sa.Column("theme", sa.Text(), nullable=False, server_default="dark"))
    op.add_column("users", sa.Column("display_name", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("default_summary_language", sa.Text(), nullable=False, server_default="auto"),
    )
    op.execute(
        sa.text(
            """
            UPDATE users
            SET
                locale = COALESCE(NULLIF(preferences_json ->> 'lang_preference', ''), locale),
                theme = COALESCE(NULLIF(preferences_json #>> '{app_settings,theme}', ''), theme),
                default_summary_language = COALESCE(NULLIF(preferences_json ->> 'lang_preference', ''), default_summary_language)
            WHERE preferences_json IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_column("users", "default_summary_language")
    op.drop_column("users", "display_name")
    op.drop_column("users", "theme")
    op.drop_column("users", "locale")
    op.drop_column("users", "onboarding_completed_at")
