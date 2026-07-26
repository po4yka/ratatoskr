"""widen summaries.version from INTEGER to BIGINT.

Legacy `BaseModel.save()` overrides `Summary.version` with the same
millisecond-resolution timestamp it uses for `server_version` (see
`app/cli/_legacy_peewee_models/_base.py:42-43`). Those values are
~1.7e12 — they fit SQLite's flexible INTEGER but overflow Postgres
INTEGER (max 2.1e9), so the SQLite -> Postgres migrator
(`app.cli.migrate_sqlite_to_postgres`) fails with
`asyncpg.DataError: value out of int32 range` on the very first
Summary insert.

Widening to BIGINT matches `Summary.server_version` (also BIGINT) and
lets every legacy row migrate unchanged. Production-relevant: the
live Pi `summaries` table has the same value pattern, so this
revision is a prerequisite for C2 (Pi cutover).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.alter_column(
        "summaries",
        "version",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default=None,
    )


_INT32_MAX = 2_147_483_647


def downgrade() -> None:
    # summaries.version carries ~1.7e12 millisecond-epoch values on legacy /
    # live-Pi data (see the upgrade docstring) that overflow int32, so narrowing
    # back to INTEGER would fail with `value out of int32 range` (or silently
    # truncate) -- reproducing the exact bug this revision fixes. Refuse when any
    # value exceeds the int32 max; Alembic wraps the downgrade in a transaction,
    # so raising leaves the column as BIGINT. On data that fits int32 (or an
    # empty table) the original narrowing still runs.
    bind = op.get_bind()
    max_version = bind.execute(sa.text("SELECT MAX(version) FROM summaries")).scalar()
    if max_version is not None and max_version > _INT32_MAX:
        msg = (
            "Cannot downgrade past 0002: summaries.version holds a value "
            f"({max_version}) above the int32 max ({_INT32_MAX}); narrowing to "
            "INTEGER would overflow. These are millisecond-epoch timestamps from "
            "legacy BaseModel.save() -- treat this revision as one-way once they "
            "exist."
        )
        raise RuntimeError(msg)

    op.alter_column(
        "summaries",
        "version",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default=None,
    )
