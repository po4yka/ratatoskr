"""graph_node llm_attempt_trigger value (ADR-0011 / migration 0036).

The pure-model assertion always runs. The Postgres assertions require a live DB
via TEST_DATABASE_URL and skip otherwise (the alembic round-trip for migration
0036 itself is exercised by the migrate-db CLI/migration tests). Schema is built
from the models, so the enum is created with its current members.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text

from app.config.database import DatabaseConfig
from app.db.base import Base
from app.db.models import ALL_MODELS, LLMAttemptTrigger
from app.db.session import Database
from tests.db_helpers_async import create_request, insert_llm_call


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


def _tables() -> list:
    return [model.__table__ for model in reversed(ALL_MODELS)]


@pytest.fixture
async def database() -> AsyncGenerator[Database]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for graph_node enum Postgres tests")
    db = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    tables = _tables()
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=tables)
        await conn.run_sync(Base.metadata.create_all, tables=list(reversed(tables)))
    try:
        yield db
    finally:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=tables)
        await db.dispose()


def test_graph_node_is_an_enum_member() -> None:
    assert LLMAttemptTrigger.graph_node.value == "graph_node"
    assert "graph_node" in {e.value for e in LLMAttemptTrigger}


async def test_graph_node_in_pg_enum_range(database: Database) -> None:
    async with database.transaction() as session:
        rows = (
            (
                await session.execute(
                    text("SELECT unnest(enum_range(NULL::llm_attempt_trigger))::text")
                )
            )
            .scalars()
            .all()
        )
    assert "graph_node" in rows


async def test_llm_call_accepts_graph_node_trigger(database: Database) -> None:
    async with database.transaction() as session:
        request_id = await create_request(
            session, type_="url", status="completed", correlation_id="t2-graph-node"
        )
        call_id = await insert_llm_call(
            session, request_id=request_id, provider="openrouter", model="x", status="success"
        )
        # The helper does not set attempt_trigger; set it explicitly to prove the
        # Postgres enum column accepts the new value (errors if not in the enum).
        await session.execute(
            text("UPDATE llm_calls SET attempt_trigger = 'graph_node' WHERE id = :id"),
            {"id": call_id},
        )

    async with database.transaction() as session:
        value = (
            await session.execute(
                text("SELECT attempt_trigger::text FROM llm_calls WHERE id = :id"),
                {"id": call_id},
            )
        ).scalar_one()
    assert value == "graph_node"
