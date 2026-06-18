"""Add a GIN index on audit_logs.details_json.

The admin audit-log view filters by user_id using JSONB containment
(``details_json @> '{"user_id": ...}'``). A GIN index on the JSONB column lets
Postgres satisfy that predicate without a sequential scan.

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-29
"""

from __future__ import annotations

from alembic import op

revision: str = "0029"
down_revision: str = "0028"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_IDX = "ix_audit_logs_details_json"


def upgrade() -> None:
    op.create_index(_IDX, "audit_logs", ["details_json"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index(_IDX, table_name="audit_logs")
