"""add webwright_tool to llm_attempt_trigger enum

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-26
"""

from __future__ import annotations

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is transaction-unsafe in older Postgres, but
    # supported as-is in 13+. The Ratatoskr deployment targets PG16, so this
    # is fine.
    op.execute(
        "ALTER TYPE llm_attempt_trigger ADD VALUE IF NOT EXISTS 'webwright_tool'"
    )


def downgrade() -> None:
    # Postgres does not support removing values from an enum without
    # recreating the type and rewriting every column that references it.
    # Operationally that's a heavy migration that should ship by itself if
    # ever needed; downgrade here is intentionally a no-op so we don't
    # half-break existing data.
    pass
