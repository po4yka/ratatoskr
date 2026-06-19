"""Programmatic Alembic runner for the PostgreSQL schema."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from alembic.config import Config
    from sqlalchemy.engine import Connection

logger = get_logger(__name__)

_INI_PATH = str(Path(__file__).resolve().parents[2] / "alembic.ini")


def _resolve_dsn(dsn: str | None) -> str:
    resolved = (dsn or os.getenv("DATABASE_URL", "")).strip()
    if not resolved:
        password = os.getenv("POSTGRES_PASSWORD", "").strip()
        if password:
            resolved = f"postgresql+asyncpg://ratatoskr_app:{password}@postgres:5432/ratatoskr"
    return resolved


def _build_alembic_config(dsn: str | None = None) -> Config:
    """Build Alembic config with the PostgreSQL asyncpg URL."""
    from alembic.config import Config

    resolved_dsn = _resolve_dsn(dsn)
    if not resolved_dsn.startswith("postgresql+asyncpg://"):
        msg = "Alembic requires a postgresql+asyncpg:// URL"
        raise RuntimeError(msg)

    cfg = Config(_INI_PATH)
    cfg.set_main_option("sqlalchemy.url", resolved_dsn)
    return cfg


def upgrade_to_head(dsn: str | None = None) -> None:
    """Run pending Alembic revisions to head."""
    from alembic import command

    cfg = _build_alembic_config(dsn)
    command.upgrade(cfg, "head")
    logger.info("alembic_upgrade_complete")


def render_upgrade_sql(dsn: str | None = None) -> None:
    """Render pending Alembic revisions as SQL without applying them."""
    from alembic import command

    cfg = _build_alembic_config(dsn)
    command.upgrade(cfg, "head", sql=True)


def assert_database_at_head(dsn: str | None = None) -> None:
    """Raise RuntimeError if the database revision is not at Alembic head."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        current_heads, script_heads = asyncio.run(_revision_state(dsn))
    else:
        msg = "Alembic head check cannot run inside an active event loop"
        raise RuntimeError(msg)

    current = set(current_heads)
    expected = set(script_heads)
    if current != expected:
        msg = (
            "Database schema is not at Alembic head "
            f"(current={sorted(current) or ['<base>']}, heads={sorted(expected)})"
        )
        raise RuntimeError(msg)
    logger.info("alembic_schema_at_head", extra={"heads": sorted(expected)})


async def _revision_state(dsn: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (database current heads, script heads)."""
    from alembic.script import ScriptDirectory
    from sqlalchemy.ext.asyncio import create_async_engine

    cfg = _build_alembic_config(dsn)
    script_heads = tuple(ScriptDirectory.from_config(cfg).get_heads())
    engine = create_async_engine(_resolve_dsn(dsn), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            current_heads = await connection.run_sync(_current_heads)
    finally:
        await engine.dispose()
    return current_heads, script_heads


def _current_heads(connection: Connection) -> tuple[str, ...]:
    from alembic.migration import MigrationContext

    context = MigrationContext.configure(connection)
    return tuple(context.get_current_heads())


def print_status(dsn: str | None = None) -> None:
    """Print current Alembic revision and migration history to stdout."""
    from alembic import command

    cfg = _build_alembic_config(dsn)
    print("Current revision:")
    command.current(cfg, verbose=True)
    print("\nMigration history:")
    command.history(cfg, verbose=False)
