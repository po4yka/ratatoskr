"""Tests for llm_calls.attempt_index and attempt_trigger columns.

These tests require a live Postgres DB via TEST_DATABASE_URL and are
skipped automatically when that env-var is absent.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select

from app.config.database import DatabaseConfig
from app.db.base import Base
from app.db.models import ALL_MODELS, LLMAttemptTrigger, LLMCall, Request
from app.db.session import Database
from app.infrastructure.persistence.repositories.llm_repository import LLMRepositoryAdapter

pytestmark = pytest.mark.postgres


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


def _tables() -> list:
    return [model.__table__ for model in reversed(ALL_MODELS)]


@pytest.fixture
async def db_with_schema() -> AsyncGenerator[Database]:
    """Spin up a fresh schema for each test and tear it down after."""
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres llm_attempt_tracking tests")

    database = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    tables = _tables()
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=tables)
        await conn.run_sync(Base.metadata.create_all, tables=list(reversed(tables)))
    try:
        yield database
    finally:
        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=tables)
        await database.dispose()


async def _create_request(database: Database) -> int:
    """Insert a minimal request row and return its id."""
    async with database.transaction() as session:
        req = Request(type="url", status="pending", correlation_id="test-corr")
        session.add(req)
        await session.flush()
        return req.id


@pytest.mark.asyncio
async def test_attempt_index_auto_increments(db_with_schema: Database) -> None:
    """Inserting two calls for the same request_id produces attempt_index 1 and 2."""
    repo = LLMRepositoryAdapter(db_with_schema)
    req_id = await _create_request(db_with_schema)

    call1_id = await repo.async_insert_llm_call(
        {
            "request_id": req_id,
            "provider": "openrouter",
            "model": "test-model",
            "status": "ok",
        }
    )
    call2_id = await repo.async_insert_llm_call(
        {
            "request_id": req_id,
            "provider": "openrouter",
            "model": "test-model",
            "status": "error",
        }
    )

    async with db_with_schema.session() as session:
        row1 = await session.scalar(select(LLMCall).where(LLMCall.id == call1_id))
        row2 = await session.scalar(select(LLMCall).where(LLMCall.id == call2_id))

    assert row1 is not None
    assert row2 is not None
    assert row1.attempt_index == 1
    assert row2.attempt_index == 2


@pytest.mark.asyncio
async def test_attempt_trigger_enum_round_trips(db_with_schema: Database) -> None:
    """All defined trigger values round-trip through SQLAlchemy correctly."""
    repo = LLMRepositoryAdapter(db_with_schema)
    req_id = await _create_request(db_with_schema)

    triggers = list(LLMAttemptTrigger)
    ids: list[int] = []
    for trigger in triggers:
        call_id = await repo.async_insert_llm_call(
            {
                "request_id": req_id,
                "provider": "openrouter",
                "model": "test-model",
                "status": "ok",
                "attempt_trigger": trigger.value,
            }
        )
        ids.append(call_id)

    async with db_with_schema.session() as session:
        for call_id, expected_trigger in zip(ids, triggers, strict=True):
            row = await session.scalar(select(LLMCall).where(LLMCall.id == call_id))
            assert row is not None, f"LLMCall {call_id} not found"
            assert row.attempt_trigger == expected_trigger.value, (
                f"Expected {expected_trigger.value!r}, got {row.attempt_trigger!r}"
            )


@pytest.mark.asyncio
async def test_attempt_trigger_defaults_to_initial(db_with_schema: Database) -> None:
    """When no trigger is supplied the column defaults to 'initial'."""
    repo = LLMRepositoryAdapter(db_with_schema)
    req_id = await _create_request(db_with_schema)

    call_id = await repo.async_insert_llm_call(
        {
            "request_id": req_id,
            "provider": "openrouter",
            "model": "test-model",
            "status": "ok",
        }
    )

    async with db_with_schema.session() as session:
        row = await session.scalar(select(LLMCall).where(LLMCall.id == call_id))

    assert row is not None
    assert row.attempt_trigger == LLMAttemptTrigger.initial.value


@pytest.mark.asyncio
async def test_user_retry_trigger_inherited_from_request(db_with_schema: Database) -> None:
    """First LLM call inherits 'user_retry' from requests.initial_attempt_trigger."""
    repo = LLMRepositoryAdapter(db_with_schema)

    # Create a request that simulates a user-initiated retry clone.
    async with db_with_schema.transaction() as session:
        req = Request(
            type="url",
            status="pending",
            correlation_id="test-corr-retry-1",
            initial_attempt_trigger="user_retry",
        )
        session.add(req)
        await session.flush()
        req_id = req.id

    call_id = await repo.async_insert_llm_call(
        {
            "request_id": req_id,
            "provider": "openrouter",
            "model": "test-model",
            "status": "ok",
        }
    )

    async with db_with_schema.session() as session:
        row = await session.scalar(select(LLMCall).where(LLMCall.id == call_id))

    assert row is not None
    assert row.attempt_index == 1
    assert row.attempt_trigger == LLMAttemptTrigger.user_retry.value
