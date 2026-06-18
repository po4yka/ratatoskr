"""Add unique constraint on llm_calls(request_id, attempt_index).

Replaces the non-unique composite index ``ix_llm_calls_request_id_attempt_index``
with a UNIQUE constraint ``uq_llm_calls_request_id_attempt_index`` on the same
columns.

Rationale
---------
CLAUDE.md documents that ``attempt_index`` is 1-based and monotonic per
``request_id``.  Without a unique constraint concurrent retry paths can race
and silently insert duplicate (request_id, attempt_index) pairs, breaking the
invariant that is assumed by every "all attempts for a request, ordered" query.

The old non-unique index is dropped after the constraint is established.

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision: str = "0009"
down_revision: str = "0008"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_NEW_CONSTRAINT = "uq_llm_calls_request_id_attempt_index"
_OLD_INDEX = "ix_llm_calls_request_id_attempt_index"
_TABLE = "llm_calls"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Guard: refuse to proceed if duplicate (request_id, attempt_index)
    # pairs already exist — the unique constraint would fail, and silent
    # deletion is worse than a clear operator error.
    # Skipped in offline (--sql) mode where no live connection exists.
    # ------------------------------------------------------------------
    if not context.is_offline_mode():
        bind = op.get_bind()
        result = bind.execute(
            sa.text(
                """
                SELECT COUNT(*) AS dup_count
                FROM (
                    SELECT request_id, attempt_index
                    FROM llm_calls
                    GROUP BY request_id, attempt_index
                    HAVING COUNT(*) > 1
                ) dups
                """
            )
        )
        row = result.fetchone()
        dup_count: int = row[0] if row else 0
        if dup_count > 0:
            duplicate_query_hint = (
                "  SELECT request_id, attempt_index, COUNT(*) "
                "  FROM llm_calls "
                "  GROUP BY request_id, attempt_index "
                "  HAVING COUNT(*) > 1;"
            )
            msg = (
                f"Migration {revision} aborted: found {dup_count} duplicate "
                "(request_id, attempt_index) pair(s) in llm_calls. "
                "Clean up duplicates before applying this migration. "
                "Example query to inspect them:\n"
                f"{duplicate_query_hint}"
            )
            raise RuntimeError(msg)

    op.create_unique_constraint(
        _NEW_CONSTRAINT,
        _TABLE,
        ["request_id", "attempt_index"],
    )

    # Drop the old non-unique index (now redundant — the unique constraint
    # index covers the same column set and is used for lookups equally).
    op.drop_index(_OLD_INDEX, table_name=_TABLE)


def downgrade() -> None:
    # Restore the old non-unique composite index and drop the constraint.
    op.create_index(
        _OLD_INDEX,
        _TABLE,
        ["request_id", "attempt_index"],
        unique=False,
    )

    op.drop_constraint(_NEW_CONSTRAINT, _TABLE, type_="unique")
