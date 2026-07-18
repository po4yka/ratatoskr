"""Unit tests for CheckpointerRuntime (no live DB, no real langgraph import).

The langgraph / psycopg_pool modules are stubbed via sys.modules so the lazy
imports inside ``start()`` resolve to mocks. The real setup/search-path/row-
factory/resume/delete/concurrent-setup contracts live in the PostgreSQL-marked
``test_checkpointer_runtime_postgres.py`` sibling. This module asserts the pool is built with the
ADR-0004 settings, that strict_msgpack toggles the pickle fallback, that
setup() runs, that stop() closes the pool, and that Database is never used
(invariant 4 / ADR-0018).
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.infrastructure.checkpointing.runtime as checkpoint_runtime
from app.infrastructure.checkpointing.cleanup import CheckpointPruneStats
from app.infrastructure.checkpointing.runtime import CheckpointerRuntime, _psycopg_dsn


def _cfg(*, schema="langgraph", dsn_override=None, strict_msgpack=True, pmin=1, pmax=5):
    return SimpleNamespace(
        langgraph_checkpoint=SimpleNamespace(
            schema_name=schema,
            dsn_override=dsn_override,
            strict_msgpack=strict_msgpack,
            retention_days=90,
            pool_min_size=pmin,
            pool_max_size=pmax,
        ),
        database=SimpleNamespace(dsn="postgresql+asyncpg://u:p@h:5432/db"),
        deployment=SimpleNamespace(is_production_mode=False),
    )


def _install_stubs(monkeypatch, *, guard_database=True, setup_error=None):
    monkeypatch.setattr(
        checkpoint_runtime,
        "prune_expired_checkpoints",
        AsyncMock(return_value=CheckpointPruneStats()),
    )
    pool = MagicMock()
    pool.open = AsyncMock()
    pool.close = AsyncMock()
    schema_conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[])
    schema_conn.execute = AsyncMock(return_value=cursor)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    schema_conn.transaction = MagicMock(return_value=tx)
    conn_cm = MagicMock()
    conn_cm.__aenter__ = AsyncMock(return_value=schema_conn)
    conn_cm.__aexit__ = AsyncMock(return_value=False)
    pool.connection = MagicMock(return_value=conn_cm)
    pool_class = MagicMock(return_value=pool)

    saver = MagicMock()
    saver.setup = AsyncMock(side_effect=setup_error)
    saver_class = MagicMock(return_value=saver)
    serde_class = MagicMock(return_value=MagicMock())

    def _mod(name: str) -> types.ModuleType:
        return types.ModuleType(name)

    stubs = {
        name: _mod(name)
        for name in (
            "langgraph",
            "langgraph.checkpoint",
            "langgraph.checkpoint.postgres",
            "langgraph.checkpoint.postgres.aio",
            "langgraph.checkpoint.serde",
            "langgraph.checkpoint.serde.jsonplus",
            "psycopg_pool",
            "psycopg",
            "psycopg.rows",
        )
    }
    stubs["langgraph.checkpoint.postgres.aio"].AsyncPostgresSaver = saver_class
    stubs["langgraph.checkpoint.serde.jsonplus"].JsonPlusSerializer = serde_class
    stubs["psycopg_pool"].AsyncConnectionPool = pool_class
    stubs["psycopg.rows"].dict_row = object()
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    if guard_database:
        monkeypatch.setattr(
            "app.db.session.Database",
            MagicMock(side_effect=AssertionError("checkpointer must not use Database")),
        )
    return SimpleNamespace(
        pool=pool,
        pool_class=pool_class,
        schema_conn=schema_conn,
        tx=tx,
        saver=saver,
        saver_class=saver_class,
        serde_class=serde_class,
    )


def test_psycopg_dsn_strips_asyncpg_suffix():
    assert _psycopg_dsn("postgresql+asyncpg://u:p@h/db", None) == "postgresql://u:p@h/db"
    assert _psycopg_dsn("ignored", "postgresql+asyncpg://o:o@h/x") == "postgresql://o:o@h/x"


def test_saver_property_raises_before_start():
    rt = CheckpointerRuntime(cfg=_cfg())
    with pytest.raises(RuntimeError):
        _ = rt.saver


async def test_stop_before_start_is_noop():
    rt = CheckpointerRuntime(cfg=_cfg())
    await rt.stop()  # must not raise


async def test_start_builds_isolated_pool_and_runs_setup(monkeypatch):
    m = _install_stubs(monkeypatch)
    rt = CheckpointerRuntime(cfg=_cfg(schema="langgraph", pmin=1, pmax=5))

    await rt.start()

    m.pool_class.assert_called_once()
    kw = m.pool_class.call_args.kwargs
    assert kw["conninfo"] == "postgresql://u:p@h:5432/db"  # +asyncpg stripped
    assert kw["min_size"] == 1 and kw["max_size"] == 5
    assert kw["open"] is False
    assert kw["kwargs"]["autocommit"] is True
    assert "row_factory" in kw["kwargs"]
    assert callable(kw["configure"])
    m.pool.open.assert_awaited_once()
    schema_sql = m.schema_conn.execute.await_args_list[0].args[0]
    assert "CREATE SCHEMA IF NOT EXISTS" in schema_sql and "langgraph" in schema_sql
    assert m.serde_class.call_args.kwargs["pickle_fallback"] is False  # strict
    assert m.saver_class.call_count == 2
    assert m.saver_class.call_args_list[0].args[0] is m.schema_conn
    assert m.saver_class.call_args_list[1].args[0] is m.pool
    m.saver.setup.assert_awaited_once()
    assert rt.saver is m.saver


async def test_start_serializes_setup_with_advisory_lock(monkeypatch):
    m = _install_stubs(monkeypatch)

    await CheckpointerRuntime(cfg=_cfg(pmax=1)).start()

    statements = [call.args[0] for call in m.schema_conn.execute.await_args_list]
    lock_index = statements.index("SELECT pg_advisory_lock(%s)")
    unlock_index = statements.index("SELECT pg_advisory_unlock(%s)")
    assert lock_index < unlock_index
    assert m.saver_class.call_args_list[0].args[0] is m.schema_conn


async def test_start_cleans_checkpoints_before_exposing_saver(monkeypatch):
    m = _install_stubs(monkeypatch)
    rt = CheckpointerRuntime(cfg=_cfg())
    events: list[str] = []

    async def setup() -> None:
        events.append("setup")

    async def cleanup(*args, **kwargs) -> CheckpointPruneStats:
        assert events == ["setup"]
        with pytest.raises(RuntimeError):
            _ = rt.saver
        events.append("cleanup")
        return CheckpointPruneStats()

    m.saver.setup = AsyncMock(side_effect=setup)
    monkeypatch.setattr(checkpoint_runtime, "prune_expired_checkpoints", cleanup)

    await rt.start()

    assert events == ["setup", "cleanup"]
    assert rt.saver is m.saver


async def test_start_closes_pool_when_startup_cleanup_fails(monkeypatch):
    m = _install_stubs(monkeypatch)
    rt = CheckpointerRuntime(cfg=_cfg())
    monkeypatch.setattr(
        checkpoint_runtime,
        "prune_expired_checkpoints",
        AsyncMock(side_effect=RuntimeError("cleanup boom")),
    )

    with pytest.raises(RuntimeError, match="cleanup boom"):
        await rt.start()

    m.pool.close.assert_awaited_once()
    with pytest.raises(RuntimeError):
        _ = rt.saver


async def test_strict_msgpack_false_enables_pickle_fallback(monkeypatch):
    m = _install_stubs(monkeypatch)
    rt = CheckpointerRuntime(cfg=_cfg(strict_msgpack=False))
    await rt.start()
    assert m.serde_class.call_args.kwargs["pickle_fallback"] is True


async def test_stop_closes_pool(monkeypatch):
    m = _install_stubs(monkeypatch)
    rt = CheckpointerRuntime(cfg=_cfg())
    await rt.start()
    await rt.stop()
    m.pool.close.assert_awaited_once()
    with pytest.raises(RuntimeError):
        _ = rt.saver


async def test_start_closes_pool_on_setup_failure(monkeypatch):
    """A failure after pool.open() must not leak the pool (failure isolation)."""
    m = _install_stubs(monkeypatch, setup_error=RuntimeError("setup boom"))
    rt = CheckpointerRuntime(cfg=_cfg())
    with pytest.raises(RuntimeError):
        await rt.start()
    m.pool.close.assert_awaited_once()  # opened pool cleaned up, not leaked
    with pytest.raises(RuntimeError):
        _ = rt.saver  # never became ready


async def test_configure_callback_pins_search_path(monkeypatch):
    """The per-connection configure callback sets search_path to the schema."""
    m = _install_stubs(monkeypatch)
    rt = CheckpointerRuntime(cfg=_cfg(schema="langgraph"))
    await rt.start()
    configure = m.pool_class.call_args.kwargs["configure"]
    conn = MagicMock()
    conn.execute = AsyncMock()
    await configure(conn)
    sql = conn.execute.await_args.args[0]
    assert "SET search_path" in sql and "langgraph" in sql
