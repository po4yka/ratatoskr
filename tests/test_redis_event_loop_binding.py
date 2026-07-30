"""The shared Redis client must not outlive the loop that created it.

A redis.asyncio pool keeps futures bound to its creating loop, and an
asyncio.Lock binds on first acquire, so reusing the process-wide client from a
second loop raises "got Future attached to a different loop" -- surfacing far
from the cause, e.g. inside the rate-limit middleware on an unrelated request.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.infrastructure import redis as redis_module


class _FakeClient:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.closed = False

    async def ping(self) -> bool:
        if asyncio.get_running_loop() is not self.loop:
            msg = "got Future attached to a different loop"
            raise RuntimeError(msg)
        return True

    async def aclose(self) -> None:
        self.closed = True


def _cfg() -> Any:
    return SimpleNamespace(
        redis=SimpleNamespace(
            enabled=True,
            url="redis://localhost:6379/0",
            host="localhost",
            port=6379,
            db=0,
            password=None,
            socket_timeout=1,
            reconnect_interval=0,
            required=False,
            prefix="t",
        )
    )


def test_client_is_rebuilt_when_the_loop_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[_FakeClient] = []

    def _from_url(*_args: Any, **_kwargs: Any) -> _FakeClient:
        client = _FakeClient(asyncio.get_event_loop())
        built.append(client)
        return client

    monkeypatch.setattr(redis_module.aioredis, "from_url", _from_url)
    manager = redis_module._RedisConnectionManager()

    async def _use() -> Any:
        return await manager.get(_cfg())

    first = asyncio.run(_use())
    second = asyncio.run(_use())

    assert first is not None
    assert second is not None
    assert len(built) == 2, "the client from the retired loop was reused"
    assert first is not second


def test_client_is_reused_within_one_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebuilding on every call would drop pooling entirely."""
    built: list[_FakeClient] = []

    def _from_url(*_args: Any, **_kwargs: Any) -> _FakeClient:
        client = _FakeClient(asyncio.get_event_loop())
        built.append(client)
        return client

    monkeypatch.setattr(redis_module.aioredis, "from_url", _from_url)
    manager = redis_module._RedisConnectionManager()

    async def _use_twice() -> tuple[Any, Any]:
        return await manager.get(_cfg()), await manager.get(_cfg())

    first, second = asyncio.run(_use_twice())

    assert first is second
    assert len(built) == 1
