"""make llm_calls.request_id nullable and add agent attempt_trigger

Revision ID: 0051
Revises: 0050
Create Date: 2026-07-14

Agent-originated LLM calls (repo analysis, multi-source aggregation, signal
judge) have no parent ``Request`` row, so ``persist_agent_llm_call`` writes them
with ``request_id=NULL``. The column was ``NOT NULL``, so every such insert
failed with an IntegrityError that the best-effort persistence path swallowed --
no agent LLM call was ever recorded (``COUNT(*) FILTER (WHERE request_id IS
NULL) == 0``), violating the "persist every LLM call" rule and losing all agent
cost/latency telemetry.

This migration makes ``request_id`` nullable and adds an ``agent`` value to the
``llm_attempt_trigger`` enum so agent-originated calls are tagged distinctly
instead of masquerading as summarize-path ``initial`` calls.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.alter_column(
        "llm_calls",
        "request_id",
        existing_type=sa.Integer(),
        nullable=True,
    )
    # ALTER TYPE ... ADD VALUE is supported on PG 13+ (deployment targets PG17).
    # The new value is added here and NOT used in the same transaction, mirroring
    # migrations 0036 and 0048.
    op.execute("ALTER TYPE llm_attempt_trigger ADD VALUE IF NOT EXISTS 'agent'")


def downgrade() -> None:
    # Restoring NOT NULL requires removing the agent-originated rows that the
    # upgrade made possible; delete them so the constraint can be re-applied
    # deterministically. This is destructive to agent LLM-call telemetry.
    op.execute("DELETE FROM llm_calls WHERE request_id IS NULL")
    op.alter_column(
        "llm_calls",
        "request_id",
        existing_type=sa.Integer(),
        nullable=False,
    )
    # Postgres cannot remove an enum value without recreating the type and
    # rewriting every referencing column; the 'agent' value is intentionally
    # left in place (same rationale as 0048).
