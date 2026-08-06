"""normalize legacy 'success' request status to 'ok'

Revision ID: 0058
Revises: 0057
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `reconcile_stuck_processing_requests` wrote status='success', which is not a
    # RequestStatus member (COMPLETED is 'ok'). Rows carrying it were invisible to
    # the terminal-status guard in `recover_interrupted_synchronous_requests`, and
    # the API had to normalise them as a legacy value on every read. Production
    # held 7 such rows against 1393 'ok'. The writer is fixed; this retires the
    # rows it already produced.
    op.execute(sa.text("UPDATE requests SET status = 'ok' WHERE status = 'success'"))


def downgrade() -> None:
    # Not reversible: 'success' and 'ok' are indistinguishable after the update,
    # so restoring it would have to guess which rows were legacy. The value was
    # never valid, and nothing reads it.
    pass
