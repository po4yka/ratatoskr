"""Unit tests for app.tasks.langgraph_prune (no live DB, no real psycopg import).

``psycopg`` is stubbed via sys.modules so the lazy import inside ``_prune_body``
resolves to a mock. Covers: the gating + Redis-lock concurrency guard in
``_run_prune``; the materialize-once + single-transaction whole-run DELETE in
``_prune_body``; retention cutoff; asyncpg DSN strip; and the invariant that the
prune NEVER routes through app.db.session.Database (invariant 4 / ADR-0018).
"""

from __future__ import annotations

import datetime as dt
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.tasks.langgraph_prune as lp
from app.tasks.langgraph_prune import CheckpointPruneStats, _prune_body, _run_prune


def _cfg(*, enabled=True, schema="langgraph", retention_days=90, dsn_override=None):
    return SimpleNamespace(
        langgraph_checkpoint=SimpleNamespace(
            enabled=enabled,
            schema_name=schema,
            retention_days=retention_days,
            dsn_override=dsn_override,
        ),
        database=SimpleNamespace(dsn="postgresql+asyncpg://u:p@h:5432/db"),
    )


def _stub_psycopg(
    monkeypatch,
    *,
    thread_ids=("c1", "c2"),
    rowcount=3,
    connect=None,
    dict_rows=False,
):
    """Install a fake ``psycopg`` module returning a transaction-aware conn mock."""
    cursor = MagicMock()
    cursor.rowcount = rowcount
    cursor.fetchall = AsyncMock(
        return_value=[
            {"correlation_id": tid} if dict_rows else (tid,)
            for tid in thread_ids
        ]
    )
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=cursor)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    connect = connect or AsyncMock(return_value=conn)
    fake = types.ModuleType("psycopg")
    fake.AsyncConnection = MagicMock()
    fake.AsyncConnection.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    return SimpleNamespace(connect=connect, conn=conn, cursor=cursor, tx=tx)


# ── _prune_body (the DELETE logic) ────────────────────────────────────────────


async def test_prune_body_disabled_returns_zero_stats(monkeypatch):
    boom = AsyncMock(side_effect=AssertionError("must not connect when disabled"))
    _stub_psycopg(monkeypatch, connect=boom)
    stats = await _prune_body(_cfg(enabled=False))
    assert stats == CheckpointPruneStats()
    boom.assert_not_called()


async def test_prune_body_materializes_once_then_deletes_three_tables(monkeypatch):
    m = _stub_psycopg(monkeypatch, thread_ids=("c1", "c2"), rowcount=7)

    stats = await _prune_body(_cfg(enabled=True, schema="langgraph"))

    assert stats == CheckpointPruneStats(checkpoints=7, checkpoint_blobs=7, checkpoint_writes=7)
    # one connection, NOT autocommit (the deletes share a transaction snapshot)
    assert m.connect.await_count == 1
    assert "autocommit" not in m.connect.await_args.kwargs
    m.tx.__aenter__.assert_awaited_once()  # ran inside an explicit transaction
    sqls = [c.args[0] for c in m.conn.execute.await_args_list]
    # exactly one SELECT (materialize) + three DELETEs
    assert sqls[0].startswith("SELECT correlation_id FROM public.requests")
    deletes = [query.as_string() for query in sqls[1:]]
    assert len(deletes) == 3
    assert any('"langgraph"."checkpoint_writes"' in statement for statement in deletes)
    assert any('"langgraph"."checkpoint_blobs"' in statement for statement in deletes)
    assert any('"langgraph"."checkpoints"' in statement for statement in deletes)
    # deletes scope by the materialized id set, not a live subquery
    assert all("thread_id = ANY(%(ids)s)" in statement for statement in deletes)
    for call in m.conn.execute.await_args_list[1:]:
        assert call.args[1] == {"ids": ["c1", "c2"]}


async def test_prune_body_accepts_runtime_dict_rows(monkeypatch):
    """The startup pool uses psycopg ``dict_row`` rather than tuple rows."""
    m = _stub_psycopg(monkeypatch, thread_ids=("c1", "c2"), dict_rows=True)

    await _prune_body(_cfg(enabled=True))

    for call in m.conn.execute.await_args_list[1:]:
        assert call.args[1] == {"ids": ["c1", "c2"]}


async def test_prune_body_no_aged_runs_skips_deletes(monkeypatch):
    m = _stub_psycopg(monkeypatch, thread_ids=())
    stats = await _prune_body(_cfg(enabled=True))
    assert stats == CheckpointPruneStats()
    # only the SELECT ran; no DELETEs issued
    assert m.conn.execute.await_count == 1


async def test_prune_body_honours_custom_schema(monkeypatch):
    m = _stub_psycopg(monkeypatch)
    await _prune_body(_cfg(enabled=True, schema="lg_ckpt"))
    deletes = [c.args[0].as_string() for c in m.conn.execute.await_args_list[1:]]
    assert all('"lg_ckpt".' in statement for statement in deletes)


async def test_prune_body_cutoff_uses_retention_days(monkeypatch):
    m = _stub_psycopg(monkeypatch)
    before = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    await _prune_body(_cfg(enabled=True, retention_days=30))
    after = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    cutoff = m.conn.execute.await_args_list[0].args[1]["cutoff"]  # the SELECT params
    assert isinstance(cutoff, dt.datetime)
    assert before <= cutoff <= after


async def test_prune_body_strips_asyncpg_dsn_suffix(monkeypatch):
    m = _stub_psycopg(monkeypatch)
    await _prune_body(_cfg(enabled=True))
    dsn = m.connect.await_args.args[0]
    assert dsn == "postgresql://u:p@h:5432/db"
    assert "+asyncpg" not in dsn


async def test_prune_body_never_uses_database(monkeypatch):
    """Invariant 4: the prune must not construct/route through Database."""
    _stub_psycopg(monkeypatch)
    db_sentinel = MagicMock(side_effect=AssertionError("prune must not use Database"))
    monkeypatch.setattr("app.db.session.Database", db_sentinel)
    await _prune_body(_cfg(enabled=True))
    db_sentinel.assert_not_called()


# ── _run_prune (gating + Redis lock guard) ────────────────────────────────────


def _patch_lock(monkeypatch, *, acquired: bool):
    lock_cm = MagicMock()
    lock_cm.__aenter__ = AsyncMock(return_value=acquired)
    lock_cm.__aexit__ = AsyncMock(return_value=False)
    lock_cls = MagicMock(return_value=lock_cm)
    monkeypatch.setattr(lp, "RedisDistributedLock", lock_cls)
    monkeypatch.setattr(lp, "get_redis", AsyncMock(return_value=MagicMock()))
    return lock_cls


async def test_run_prune_disabled_skips_redis_and_body(monkeypatch):
    redis = AsyncMock(side_effect=AssertionError("must not touch redis when disabled"))
    monkeypatch.setattr(lp, "get_redis", redis)
    body = AsyncMock(side_effect=AssertionError("must not run body when disabled"))
    monkeypatch.setattr(lp, "_prune_body", body)
    stats = await _run_prune(_cfg(enabled=False))
    assert stats == CheckpointPruneStats()
    redis.assert_not_called()
    body.assert_not_called()


async def test_run_prune_lock_held_skips_body(monkeypatch):
    _patch_lock(monkeypatch, acquired=False)
    body = AsyncMock(side_effect=AssertionError("must not run body when lock held"))
    monkeypatch.setattr(lp, "_prune_body", body)
    stats = await _run_prune(_cfg(enabled=True))
    assert stats == CheckpointPruneStats()
    body.assert_not_called()


async def test_run_prune_lock_acquired_delegates_to_body(monkeypatch):
    _patch_lock(monkeypatch, acquired=True)
    sentinel = CheckpointPruneStats(checkpoints=42)
    body = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(lp, "_prune_body", body)
    stats = await _run_prune(_cfg(enabled=True))
    assert stats is sentinel
    body.assert_awaited_once()
