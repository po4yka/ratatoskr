"""Runtime database resolution helpers — usable by any layer without importing the DI layer.

This module contains the functions that need to be callable from infrastructure
persistence helpers (``app.infrastructure.*``) and other runtime modules that
are not allowed to import the DI layer.

The DI layer's ``database`` module re-exports all names from here for existing callers.
"""

from __future__ import annotations

import asyncio
import threading
from functools import lru_cache
from typing import cast

from app.config import DatabaseConfig
from app.core.logging_utils import get_logger
from app.db.api_runtime_holder import _read_api_runtime
from app.db.session import Database

logger = get_logger(__name__)

_cached_runtime_db_holder: list[Database | None] = [None]
_cached_runtime_db_lock = threading.Lock()


def get_or_create_runtime_database_from_env(
    *,
    connect: bool = False,
    migrate: bool = True,
) -> Database:
    """Lazily build the shared API database outside FastAPI lifespan when needed."""
    cached = _cached_runtime_db_holder[0]
    if cached is not None:
        if connect:
            asyncio.run(cached.healthcheck())
        return cached

    with _cached_runtime_db_lock:
        cached = _cached_runtime_db_holder[0]
        if cached is not None:
            if connect:
                asyncio.run(cached.healthcheck())
            return cached

        db = Database(config=_get_env_db_config())
        if migrate:
            asyncio.run(db.migrate())
        if connect:
            asyncio.run(db.healthcheck())
        _cached_runtime_db_holder[0] = db
        logger.info("runtime_database_initialized")
        return db


def clear_cached_runtime_database() -> None:
    """Reset the fallback runtime DB cache used outside managed lifespans."""
    cached = _cached_runtime_db_holder[0]
    if cached is not None:
        asyncio.run(cached.dispose())
    _cached_runtime_db_holder[0] = None
    cache_clear = getattr(_get_env_db_config, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


def resolve_runtime_database() -> Database:
    """Resolve the process runtime Database without importing app.api.

    Mirrors the no-request behavior of
    ``app.api.dependencies.database.get_session_manager``: prefer the active API
    runtime's database when one is set, else fall back to the env-configured
    cached database. Used by infrastructure persistence helpers that run in both
    the API process and the worker/bot processes, so they no longer have to reach
    into the API layer for a session manager.
    """
    runtime = _read_api_runtime()
    if runtime is not None:
        return cast("Database", runtime.db)
    return get_or_create_runtime_database_from_env(migrate=True)


@lru_cache(maxsize=1)
def _get_env_db_config() -> DatabaseConfig:
    return DatabaseConfig()
