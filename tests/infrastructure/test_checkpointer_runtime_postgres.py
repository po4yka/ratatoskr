"""Live PostgreSQL contract tests for the LangGraph checkpointer runtime."""

from __future__ import annotations

import asyncio
import operator
import os
import uuid
from types import SimpleNamespace
from typing import Annotated, TypedDict

import pytest

from app.infrastructure.checkpointing.runtime import CheckpointerRuntime, _psycopg_dsn

pytestmark = pytest.mark.postgres


class _ResumeState(TypedDict):
    trail: Annotated[list[str], operator.add]


def _database_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for live checkpointer tests")
    return dsn


def _schema() -> str:
    return f"langgraph_test_{uuid.uuid4().hex}"


def _cfg(schema: str, *, pool_max_size: int = 3) -> SimpleNamespace:
    dsn = _database_dsn()
    return SimpleNamespace(
        langgraph_checkpoint=SimpleNamespace(
            schema_name=schema,
            dsn_override=None,
            strict_msgpack=True,
            retention_days=90,
            pool_min_size=1,
            pool_max_size=pool_max_size,
        ),
        database=SimpleNamespace(dsn=dsn),
        deployment=SimpleNamespace(is_production_mode=False),
    )


async def _drop_schema(schema: str) -> None:
    from psycopg import AsyncConnection, sql

    async with await AsyncConnection.connect(
        _psycopg_dsn(_database_dsn(), None), autocommit=True
    ) as conn:
        await conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )


def _resume_graph(checkpointer):
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(_ResumeState)
    builder.add_node("a", lambda _state: {"trail": ["a"]})
    builder.add_node("b", lambda _state: {"trail": ["b"]})
    builder.add_edge(START, "a")
    builder.add_edge("a", "b")
    builder.add_edge("b", END)
    return builder.compile(checkpointer=checkpointer, interrupt_after=["a"])


async def test_live_setup_search_path_row_factory_resume_and_delete() -> None:
    schema = _schema()
    runtime = CheckpointerRuntime(cfg=_cfg(schema))
    try:
        await runtime.start()

        # The actual pool contract matters: saver queries expect mapping rows and
        # every borrowed connection must resolve unqualified tables in this schema.
        assert runtime._pool is not None
        async with runtime._pool.connection() as conn:
            cursor = await conn.execute("SHOW search_path")
            row = await cursor.fetchone()
            assert isinstance(row, dict)
            assert row["search_path"] == schema

            cursor = await conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s",
                (schema,),
            )
            tables = {row["tablename"] for row in await cursor.fetchall()}
            assert {"checkpoints", "checkpoint_blobs", "checkpoint_writes"} <= tables

        thread_id = f"resume-{uuid.uuid4().hex}"
        config = {"configurable": {"thread_id": thread_id}}
        graph = _resume_graph(runtime.saver)

        first = await graph.ainvoke({"trail": []}, config)
        assert first["trail"] == ["a"]
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("b",)

        resumed = await graph.ainvoke(None, config)
        assert resumed["trail"] == ["a", "b"]
        assert await runtime.saver.aget_tuple(config) is not None

        await runtime.saver.adelete_thread(thread_id)
        assert await runtime.saver.aget_tuple(config) is None
        async with runtime._pool.connection() as conn:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                cursor = await conn.execute(
                    f"SELECT count(*) AS count FROM {table} WHERE thread_id = %s",
                    (thread_id,),
                )
                assert (await cursor.fetchone())["count"] == 0
    finally:
        await runtime.stop()
        await _drop_schema(schema)


async def test_live_concurrent_setup_is_serialized() -> None:
    schema = _schema()
    runtimes = [CheckpointerRuntime(cfg=_cfg(schema, pool_max_size=1)) for _ in range(2)]
    try:
        await asyncio.gather(*(runtime.start() for runtime in runtimes))
        assert all(runtime.saver is not None for runtime in runtimes)
    finally:
        await asyncio.gather(*(runtime.stop() for runtime in runtimes))
        await _drop_schema(schema)
