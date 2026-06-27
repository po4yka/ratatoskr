"""add ru_translation to llm_attempt_trigger enum

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-27

Adds the ``ru_translation`` value to the ``llm_attempt_trigger`` Postgres enum so
the structured Russian translation issued by the bilingual post-summary step
(``SUMMARY_BILINGUAL_ENABLED``) can be tagged in ``llm_calls`` for cost/audit.
"""

from __future__ import annotations

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is supported on PG 13+ (the Ratatoskr deployment
    # targets PG16). The new value is added here and NOT used in the same
    # transaction, mirroring migration 0036.
    op.execute("ALTER TYPE llm_attempt_trigger ADD VALUE IF NOT EXISTS 'ru_translation'")


def downgrade() -> None:
    # Postgres cannot remove an enum value without recreating the type and
    # rewriting every referencing column; downgrade is intentionally a no-op
    # (same rationale as 0036).
    pass
