"""add normalized crawl result content text

Revision ID: 0055
Revises: 0054
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("crawl_results", sa.Column("content_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("crawl_results", "content_text")
