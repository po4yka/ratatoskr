"""add graph_node to llm_attempt_trigger enum

Revision ID: 0036
Revises: 0035
Create Date: 2026-06-15

Adds the ``graph_node`` value to the ``llm_attempt_trigger`` Postgres enum so
LLM calls issued by LangGraph summarize-graph nodes can be tagged
(ADR-0001 / ADR-0011). The value is reserved ahead of the graph cutover; no
active code path writes it yet.
"""

from __future__ import annotations

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is transaction-unsafe on older Postgres but is
    # supported in 13+ (the new value is added here and NOT used in the same
    # transaction). The Ratatoskr deployment targets PG16, so this is fine
    # inside Alembic's per-migration transaction — same pattern as 0027.
    op.execute("ALTER TYPE llm_attempt_trigger ADD VALUE IF NOT EXISTS 'graph_node'")


def downgrade() -> None:
    # Postgres cannot remove an enum value without recreating the type and
    # rewriting every referencing column. That is a heavy migration that should
    # ship on its own if ever needed; downgrade is intentionally a no-op so we
    # do not half-break existing data (same rationale as 0027).
    pass
