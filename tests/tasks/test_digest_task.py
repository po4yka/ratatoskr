"""Tests for app.tasks.digest — channel digest task body."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _stub_taskiq(monkeypatch):
    """Stub taskiq and taskiq_redis so imports work without either installed."""
    for mod_name in (
        "taskiq",
        "taskiq.abc",
        "taskiq.abc.schedule_source",
        "taskiq.scheduler",
        "taskiq.scheduler.scheduled_task",
        "taskiq.message",
        "taskiq_redis",
    ):
        if mod_name not in sys.modules:
            monkeypatch.setitem(sys.modules, mod_name, types.ModuleType(mod_name))

    taskiq_mod = sys.modules["taskiq"]
    taskiq_mod.AsyncBroker = object  # base class used in broker.py type annotation
    taskiq_mod.TaskiqDepends = lambda fn, **_kw: None
    taskiq_mod.TaskiqMiddleware = object
    taskiq_mod.InMemoryBroker = MagicMock
    taskiq_mod.TaskiqScheduler = MagicMock

    msg_mod = sys.modules["taskiq.message"]
    msg_mod.TaskiqMessage = object

    sched_task_mod = sys.modules["taskiq.scheduler.scheduled_task"]
    sched_task_mod.ScheduledTask = MagicMock

    source_mod = sys.modules["taskiq.abc.schedule_source"]
    source_mod.ScheduleSource = object

    tkr_mod = sys.modules["taskiq_redis"]
    tkr_mod.RedisStreamBroker = MagicMock
    tkr_mod.RedisAsyncResultBackend = MagicMock


def _build_cfg(*, digest_enabled: bool = True):
    return SimpleNamespace(
        digest=SimpleNamespace(
            enabled=digest_enabled,
            digest_times=["10:00", "19:00"],
            timezone="UTC",
        ),
        rss=SimpleNamespace(
            enabled=False, poll_interval_minutes=30, auto_summarize=True, max_items_per_poll=20
        ),
        signal_ingestion=SimpleNamespace(enabled=False, any_enabled=False),
        openrouter=SimpleNamespace(api_key="k", model="m", fallback_models=[]),
        telegram=SimpleNamespace(api_id=1, api_hash="h", bot_token="t:tok", allowed_user_ids=[123]),
        redis=SimpleNamespace(enabled=False),
    )


@pytest.mark.asyncio
async def test_digest_body_starts_and_stops_userbot(monkeypatch):
    _stub_taskiq(monkeypatch)

    # Evict cached modules.
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    class FakeUserbot:
        start = AsyncMock()
        stop = AsyncMock()

    class FakeLLMClient:
        aclose = AsyncMock()

    class FakeBotCtx:
        send_message = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class FakeDigestService:
        async def async_get_users_with_subscriptions(self):
            return [1001]

        async def generate_digest(self, **kwargs):
            return SimpleNamespace(post_count=2, errors=[])

    userbot = FakeUserbot()
    llm_client = FakeLLMClient()
    bot_ctx = FakeBotCtx()

    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    import app.tasks.deps as deps_mod

    monkeypatch.setattr(deps_mod, "create_digest_userbot", lambda _cfg: userbot)
    monkeypatch.setattr(deps_mod, "create_digest_llm_client", lambda _cfg: llm_client)
    monkeypatch.setattr(deps_mod, "create_digest_bot_client", lambda _cfg: bot_ctx)
    monkeypatch.setattr(
        deps_mod,
        "create_digest_service",
        lambda _cfg, **_kw: FakeDigestService(),
    )

    from app.tasks.digest import _channel_digest_body

    await _channel_digest_body(_build_cfg())

    userbot.start.assert_awaited_once()
    userbot.stop.assert_awaited_once()
    llm_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_digest_body_stops_userbot_on_per_user_failure(monkeypatch):
    """Userbot must be stopped even when a per-user generate_digest() call raises."""
    _stub_taskiq(monkeypatch)
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    class FakeUserbot:
        start = AsyncMock()
        stop = AsyncMock()

    class FakeLLMClient:
        aclose = AsyncMock()

    class FakeBotCtx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    class BrokenDigestService:
        async def async_get_users_with_subscriptions(self):
            return [42]

        async def generate_digest(self, **kwargs):
            raise RuntimeError("LLM quota exceeded")

    userbot = FakeUserbot()
    llm_client = FakeLLMClient()

    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    import app.tasks.deps as deps_mod

    monkeypatch.setattr(deps_mod, "create_digest_userbot", lambda _: userbot)
    monkeypatch.setattr(deps_mod, "create_digest_llm_client", lambda _: llm_client)
    monkeypatch.setattr(deps_mod, "create_digest_bot_client", lambda _: FakeBotCtx())
    monkeypatch.setattr(
        deps_mod, "create_digest_service", lambda _cfg, **_kw: BrokenDigestService()
    )

    from app.tasks.digest import _channel_digest_body

    # Per-user failure is caught; the outer function completes without raising.
    await _channel_digest_body(_build_cfg())

    userbot.stop.assert_awaited_once()
    llm_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_digest_body_uses_distributed_lock_and_skips_when_held(monkeypatch):
    # The scheduled digest must acquire the shared RedisDistributedLock -- which
    # renews the TTL via a background heartbeat -- with the digest key and TTL, not
    # a one-shot fixed-TTL lock. When another worker holds it, the run is skipped.
    _stub_taskiq(monkeypatch)
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    from app.tasks import digest as digest_mod

    captured: dict[str, object] = {}

    class _FakeLock:
        def __init__(self, client, key, ttl):
            captured["client"] = client
            captured["key"] = key
            captured["ttl"] = ttl

        async def __aenter__(self):
            return False  # another worker already holds the lock

        async def __aexit__(self, *_args):
            return False

    sentinel_client = object()
    create_userbot = MagicMock()
    monkeypatch.setattr(digest_mod, "get_redis", AsyncMock(return_value=sentinel_client))
    monkeypatch.setattr(digest_mod, "RedisDistributedLock", _FakeLock)
    monkeypatch.setattr(digest_mod, "create_digest_userbot", create_userbot)

    await digest_mod._channel_digest_body(_build_cfg())

    assert captured["client"] is sentinel_client
    assert captured["key"] == digest_mod._LOCK_KEY
    assert captured["ttl"] == digest_mod._LOCK_TTL_SECONDS
    assert digest_mod._LOCK_TTL_SECONDS == 600
    # Lock held elsewhere -> the run short-circuits before any work.
    create_userbot.assert_not_called()
