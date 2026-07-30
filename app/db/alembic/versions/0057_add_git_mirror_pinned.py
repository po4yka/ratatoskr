"""mark git mirrors the user requested by hand

Revision ID: 0057
Revises: 0056
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows default to false: everything already in the table was either
    # auto-enrolled from a GitHub listing or registered before this distinction
    # existed, and treating them as pinned would disable the unstar sweep for the
    # whole table.
    op.add_column(
        "git_mirrors",
        sa.Column("pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("git_mirrors", "pinned")
