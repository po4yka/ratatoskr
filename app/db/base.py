"""SQLAlchemy declarative base for Ratatoskr models."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, event, select
from sqlalchemy.orm import DeclarativeBase

from app.db.types import _next_server_version, _utcnow


class Base(DeclarativeBase):
    """Base class for SQLAlchemy 2.0 typed declarative models."""


@event.listens_for(Base, "before_update", propagate=True)
def _update_timestamps_and_server_version(mapper: Any, connection: Any, target: Any) -> None:
    now = _utcnow()
    if hasattr(target, "updated_at"):
        target.updated_at = now
    if hasattr(target, "server_version"):
        # Guard against the row's currently-committed value, not the
        # possibly-stale value loaded into `target` earlier in this
        # session. FOR UPDATE locks the row so a concurrent writer on the
        # same row blocks here until this transaction commits, then reads
        # the value it just committed -- guaranteeing every commit's
        # server_version is strictly greater than the one it overwrote.
        pk_columns = mapper.primary_key
        pk_values = mapper.primary_key_from_instance(target)
        where_clause = and_(
            *(column == value for column, value in zip(pk_columns, pk_values, strict=True))
        )
        current = connection.execute(
            select(mapper.local_table.c.server_version).where(where_clause).with_for_update()
        ).scalar_one()
        next_version = _next_server_version(now)
        if next_version <= current:
            next_version = current + 1
        target.server_version = next_version
