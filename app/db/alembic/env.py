"""Alembic environment for the SQLAlchemy 2.0 Postgres schema."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import TYPE_CHECKING

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.db.models import Base

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False is load-bearing, not cosmetic.
    #
    # fileConfig defaults to True, and alembic.ini declares only
    # root/sqlalchemy/alembic — so every OTHER logger that already exists gets
    # Logger.disabled = True. Migrations run in-process (Database.migrate ->
    # bootstrap -> command.upgrade), so after the first migration every `app.*`
    # module logger created at import time is silently dead for the rest of the
    # process: a disabled logger drops records inside Logger.handle(), and
    # nothing downstream ever sees them.
    #
    # That is invisible in production (the loggers are re-created per process)
    # but it silently disarmed four log-assertion tests, which passed alone and
    # failed after any DB-backed test had migrated first. pytest cannot undo it
    # either — caplog only re-enables what its own level handling turned off.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _get_db_url() -> str:
    """Resolve the asyncpg Postgres URL for CLI and programmatic Alembic runs."""
    url = config.get_main_option("sqlalchemy.url", "").strip()
    if url and url != "postgresql+asyncpg://":
        return url
    env_url = os.getenv("DATABASE_URL", "").strip()
    if env_url:
        return env_url
    password = os.getenv("POSTGRES_PASSWORD", "").strip()
    if password:
        return f"postgresql+asyncpg://ratatoskr_app:{password}@postgres:5432/ratatoskr"
    msg = "DATABASE_URL must be set to a postgresql+asyncpg:// URL for Alembic"
    raise RuntimeError(msg)


def _require_asyncpg_url(url: str) -> str:
    if not url.startswith("postgresql+asyncpg://"):
        msg = "Alembic requires a postgresql+asyncpg:// URL"
        raise RuntimeError(msg)
    return url


def run_migrations_offline() -> None:
    """Run migrations against a URL without a live connection (--sql mode)."""
    url = _require_asyncpg_url(_get_db_url())
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations with an asyncpg connection."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _require_asyncpg_url(_get_db_url())
    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations with a live asyncpg connection."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_async_migrations())
        return

    if loop.is_running():
        msg = "Alembic command API cannot run inside an active event loop"
        raise RuntimeError(msg)
    loop.run_until_complete(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
