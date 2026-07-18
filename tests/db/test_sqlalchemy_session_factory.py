from __future__ import annotations

import os
from typing import cast

import pytest
from sqlalchemy import Integer, String, Table, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import Mapped, mapped_column

from app.config.database import DatabaseConfig
from app.db.base import Base
from app.db.session import Database, get_session_for_request, with_serialization_retry


class Ping(AsyncAttrs, Base):
    __tablename__ = "test_sqlalchemy_session_ping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message: Mapped[str] = mapped_column(String(50), nullable=False)


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


def test_database_config_derives_compose_dsn_from_postgres_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    config = DatabaseConfig()

    assert config.dsn == "postgresql+asyncpg://ratatoskr_app:secret@postgres:5432/ratatoskr"
    assert config.pool_size == 8
    assert config.max_overflow == 4


@pytest.mark.asyncio
async def test_with_serialization_retry_retries_retryable_sqlstates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def no_sleep(_delay: float) -> None:
        return None

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            orig = type("Orig", (), {"sqlstate": "40001"})()
            raise OperationalError("SELECT 1", {}, orig)
        return "ok"

    monkeypatch.setattr("app.db.session.asyncio.sleep", no_sleep)

    result = await with_serialization_retry(operation)()

    assert result == "ok"
    assert calls == 3


@pytest.mark.asyncio
async def test_with_serialization_retry_does_not_retry_non_serialization_errors() -> None:
    calls = 0

    async def operation() -> None:
        nonlocal calls
        calls += 1
        orig = type("Orig", (), {"sqlstate": "23505"})()
        raise OperationalError("INSERT", {}, orig)

    with pytest.raises(OperationalError):
        await with_serialization_retry(operation)()

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_database_session_round_trip_against_postgres() -> None:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres session smoke test")

    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    ping_table = cast("Table", Ping.__table__)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=[ping_table])
            await connection.run_sync(Base.metadata.create_all, tables=[ping_table])

        async for session in get_session_for_request(database):
            session.add(Ping(message="pong"))

        async with database.session() as session:
            value = await session.scalar(select(Ping.message).where(Ping.message == "pong"))

        assert value == "pong"
    finally:
        async with database.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all, tables=[ping_table])
        await database.dispose()
