"""Retired migration (no-op).

This revision previously granted ``TRIGGER`` on ``repositories`` to the
``ratatoskr`` role for a vector-ETL integration that has since been removed.
The grant is no longer needed. The revision is retained as a no-op so the
migration chain (0009 revises 0008) stays intact; any grant left on an
already-migrated database is inert and harmless.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-11
"""

from __future__ import annotations

revision: str = "0008"
down_revision: str = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
