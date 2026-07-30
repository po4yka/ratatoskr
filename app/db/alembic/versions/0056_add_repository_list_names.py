"""add star-list membership to repositories

Revision ID: 0056
Revises: 0055
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column("list_names", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # GIN answers the containment predicate the list filter uses
    # (list_names @> '["Android"]') without scanning the table.
    op.create_index(
        "ix_repositories_list_names_gin",
        "repositories",
        ["list_names"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_repositories_list_names_gin", table_name="repositories")
    op.drop_column("repositories", "list_names")
