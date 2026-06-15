"""Unit tests for app.tasks.langgraph_prune (no live DB, no real psycopg import).

``psycopg`` is stubbed via sys.modules so the lazy import inside ``_prune_body``
resolves to a mock (the module is import-safe without the optional graph extra).
Covers the flag-off short-circuit, the whole-run DELETE across the three
checkpoint tables, retention-cutoff derivation, the asyncpg DSN strip, and the
invariant that the prune NEVER routes through app.db.session.Database
(invariant 4 / ADR-0018).
"""

from __future__ import annotations

import datetime as dt
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.tasks.langgraph_prune import CheckpointPruneStats, _prune_body


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


def _stub_psycopg(monkeypatch, *, rowcount=3, connect=None):
    """Install a fake ``psycopg`` module; return (connect_mock, conn_mock)."""
    cursor = MagicMock()
    cursor.rowcount = rowcount
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=cursor)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    connect = connect or AsyncMock(return_value=conn)
    fake = types.ModuleType("psycopg")
    fake.AsyncConnection = MagicMock()
    fake.AsyncConnection.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    return connect, conn


async def test_prune_body_disabled_returns_zero_stats(monkeypatch):
    boom = AsyncMock(side_effect=AssertionError("must not connect when disabled"))
    _stub_psycopg(monkeypatch, connect=boom)

    stats = await _prune_body(_cfg(enabled=False))

    assert stats == CheckpointPruneStats()
    boom.assert_not_called()


async def test_prune_body_deletes_all_three_tables(monkeypatch):
    connect, conn = _stub_psycopg(monkeypatch, rowcount=7)

    stats = await _prune_body(_cfg(enabled=True, schema="langgraph"))

    assert stats == CheckpointPruneStats(checkpoints=7, checkpoint_blobs=7, checkpoint_writes=7)
    assert connect.await_count == 1
    assert connect.await_args.kwargs.get("autocommit") is True
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert len(sqls) == 3
    assert any('"langgraph".checkpoint_writes' in s for s in sqls)
    assert any('"langgraph".checkpoint_blobs' in s for s in sqls)
    assert any('"langgraph".checkpoints' in s for s in sqls)
    # every DELETE scopes by run age via the parent requests row (thread_id).
    assert all("thread_id IN (" in s for s in sqls)
    assert all("public.requests" in s for s in sqls)


async def test_prune_body_honours_custom_schema(monkeypatch):
    _, conn = _stub_psycopg(monkeypatch)
    await _prune_body(_cfg(enabled=True, schema="lg_ckpt"))
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert all('"lg_ckpt".' in s for s in sqls)


async def test_prune_body_cutoff_uses_retention_days(monkeypatch):
    _, conn = _stub_psycopg(monkeypatch)
    before = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)

    await _prune_body(_cfg(enabled=True, retention_days=30))

    after = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    cutoff = conn.execute.await_args_list[0].args[1]["cutoff"]
    assert isinstance(cutoff, dt.datetime)
    assert before <= cutoff <= after


async def test_prune_body_strips_asyncpg_dsn_suffix(monkeypatch):
    connect, _ = _stub_psycopg(monkeypatch)
    await _prune_body(_cfg(enabled=True))
    dsn = connect.await_args.args[0]
    assert dsn == "postgresql://u:p@h:5432/db"
    assert "+asyncpg" not in dsn


async def test_prune_body_never_uses_database(monkeypatch):
    """Invariant 4: the prune must not construct/route through Database."""
    _stub_psycopg(monkeypatch)
    db_sentinel = MagicMock(side_effect=AssertionError("prune must not use Database"))
    monkeypatch.setattr("app.db.session.Database", db_sentinel)

    await _prune_body(_cfg(enabled=True))

    db_sentinel.assert_not_called()
