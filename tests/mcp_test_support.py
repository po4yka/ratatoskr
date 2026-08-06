"""MCP-specific pytest plugin: provides the `mcp_test_db` fixture.

Several MCP-targeted test modules declare this module as a `pytest_plugins`
entry. The legacy version of this file built a sqlite DatabaseSessionManager
and pinned the peewee `database_proxy` at it; the async port exposes a
`Database` (the new SQLAlchemy entry point) backed by `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest_asyncio

from app.config.database import DatabaseConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from app.db.session import Database


@pytest_asyncio.fixture
async def mcp_test_db(monkeypatch) -> AsyncGenerator[Database]:
    """Function-scoped async `Database` for MCP tests.

    Pytest function-scoping is required because pytest-asyncio in `auto`
    mode runs each test on a fresh event loop -- an asyncpg pool bound
    to a previous loop fails with "attached to a different loop".

    `RATATOSKR_DATABASE_NULL_POOL` is set for the same reason: some MCP
    tests are synchronous and drive the DB from a second loop (their own
    `asyncio.run` seeding plus the Starlette `TestClient` portal loop), so
    no connection may be pooled across loops. This mirrors the API `db`
    fixture in `tests/api/conftest.py`.

    Truncates every table before yielding so each test starts from a
    known empty state, mirroring the behaviour of the conftest-level
    `session` fixture.
    """
    import pytest

    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for MCP tests")

    from sqlalchemy import text as sql_text

    from app.db.base import Base
    from app.db.session import Database

    monkeypatch.setenv("RATATOSKR_DATABASE_NULL_POOL", "1")

    db = Database(config=DatabaseConfig(dsn=dsn, pool_size=2, max_overflow=2))
    await db.migrate()

    # Only tables that exist in the database: other test modules register
    # throwaway models against the shared `Base` metadata (for example
    # `test_sqlalchemy_session_ping`), and those are never migrated into
    # Postgres. Mirrors `_truncate_all_tables` in `tests/api/conftest.py`.
    async with db.session() as lookup:
        existing_rows = await lookup.execute(
            sql_text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        existing_tables = {row[0] for row in existing_rows}

    table_names = [
        t.name for t in reversed(Base.metadata.sorted_tables) if t.name in existing_tables
    ]
    if table_names:
        quoted = ", ".join(f'"{name}"' for name in table_names)
        async with db.transaction() as cleanup:
            await cleanup.execute(sql_text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))

    try:
        yield db
    finally:
        await db.dispose()


async def create_mcp_user(
    db: Database,
    *,
    telegram_user_id: int,
    username: str | None = None,
    is_owner: bool = False,
) -> None:
    """Insert a `users` row through the `mcp_test_db` `Database`.

    MCP tests hold a `Database` rather than the shared `session` fixture, so
    they cannot call `tests.db_helpers_async.upsert_user` directly. This opens
    the transaction for them and delegates to that helper.
    """
    from tests.db_helpers_async import upsert_user

    async with db.transaction() as session:
        await upsert_user(
            session,
            telegram_user_id=telegram_user_id,
            username=username,
            is_owner=is_owner,
        )
