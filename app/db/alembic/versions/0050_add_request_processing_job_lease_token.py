"""Add a fencing token to durable request-processing leases.

Revision ID: 0050
Revises: 0049
Create Date: 2026-07-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_processing_jobs",
        sa.Column("lease_token", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("request_processing_jobs", "lease_token", server_default=None)


def downgrade() -> None:
    op.drop_column("request_processing_jobs", "lease_token")
