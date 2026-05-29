"""Database dependency wiring.

``build_runtime_database`` lives here (DI-layer only — takes an ``AppConfig``).
The runtime-resolution helpers that may be called from infrastructure modules
live in ``app.db.runtime_database``; they are re-exported here so existing
``from app.di.database import resolve_runtime_database`` call sites keep working.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.db.runtime_database import (
    clear_cached_runtime_database,
    get_or_create_runtime_database_from_env,
    resolve_runtime_database,
)
from app.db.session import Database

if TYPE_CHECKING:
    from app.config import AppConfig


def build_runtime_database(
    cfg: AppConfig,
    *,
    connect: bool = False,
    migrate: bool = False,
    self_heal: bool = False,
) -> Database:
    """Create the runtime SQLAlchemy database facade from application config."""
    del self_heal
    db = Database(config=cfg.database)
    if connect:
        asyncio.run(db.healthcheck())
    if migrate:
        asyncio.run(db.migrate())
    return db


__all__ = [
    "build_runtime_database",
    "clear_cached_runtime_database",
    "get_or_create_runtime_database_from_env",
    "resolve_runtime_database",
]
