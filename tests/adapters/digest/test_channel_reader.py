from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.digest.channel_reader import (
    ChannelReader,
    _channel_source_due,
    _max_items_per_run,
)
from app.adapters.digest.userbot_client import UserbotClient
from app.config import AppConfig
from app.core.time_utils import UTC
from app.infrastructure.persistence.digest_store import DigestStore


class _FakeStore(DigestStore):
    def __init__(
        self, subscriptions: list[Any] | None = None, delivered: set[int] | None = None
    ) -> None:
        self.subscriptions = subscriptions or []
        self.delivered = delivered or set()
        self.persisted: list[tuple[Any, list[dict[str, Any]]]] = []
        self.errors = 0
        self.last_error: tuple[Any, str, int] | None = None

    async def async_list_fetchable_subscriptions(self, user_id: int) -> list[Any]:
        return self.subscriptions

    async def async_get_channel_run_state(self, *, user_id: int, channel: Any) -> dict[str, Any]:
        return {"max_items_per_run": 2, "is_active": True, "active_subscription": True}

    async def async_persist_posts(self, channel: Any, posts: list[dict[str, Any]]) -> None:
        self.persisted.append((channel, posts))

    async def async_mirror_posts_to_signal_sources(
        self, *, user_id: int, channel: Any, posts: list[dict[str, Any]]
    ) -> None:
        return None

    async def async_update_channel_fetch_success(self, channel: Any) -> None:
        return None

    async def async_list_delivered_message_ids(self, user_id: int) -> set[int]:
        return self.delivered

    async def async_record_channel_fetch_error(
        self, channel: Any, reason: str, *, max_errors: int
    ) -> bool:
        self.errors += 1
        self.last_error = (channel, reason, max_errors)
        return True


class _FakeUserbot(UserbotClient):
    def __init__(self, posts_by_username: dict[str, list[dict[str, Any]] | Exception]) -> None:
        self.posts_by_username = posts_by_username

    async def fetch_channel_posts(
        self, username: str, hours_lookback: int = 24, min_length: int = 100
    ) -> list[dict[str, Any]]:
        value = self.posts_by_username[username]
        if isinstance(value, Exception):
            raise value
        return [dict(post) for post in value]


class _Cfg(AppConfig):
    def __init__(self) -> None:
        object.__setattr__(
            self,
            "digest",
            SimpleNamespace(
                max_posts_per_digest=4,
                hours_lookback=24,
                min_post_length=20,
                max_posts_per_channel=2,
                max_fetch_errors=3,
            ),
        )


def _channel(channel_id: int, username: str, *, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=channel_id,
        channel_id=channel_id + 100,
        username=username,
        is_active=active,
        fetch_error_count=0,
    )


def _reader(store: _FakeStore, userbot: _FakeUserbot) -> ChannelReader:
    subject = ChannelReader(_Cfg(), userbot)
    subject._store = store
    return subject


def test_run_state_helpers_handle_limits_and_backoff() -> None:
    assert _max_items_per_run({"max_items_per_run": "3"}) == 3
    assert _max_items_per_run({"max_items_per_run": "bad"}) is None
    assert not _channel_source_due({"is_active": False})
    assert not _channel_source_due({"active_subscription": False})
    assert not _channel_source_due({"backoff_until": datetime.now(UTC) + timedelta(hours=1)})
    assert _channel_source_due({"backoff_until": datetime.now(UTC) - timedelta(hours=1)})


def test_fair_distribute_sorts_caps_and_fills_overflow() -> None:
    result = ChannelReader._fair_distribute(
        {
            1: [{"message_id": 1, "date": "2026-01-01"}, {"message_id": 2, "date": "2026-01-03"}],
            2: [
                {"message_id": 3, "date": "2026-01-02"},
                {"message_id": 4, "date": "2026-01-04"},
                {"message_id": 5, "date": "2026-01-05"},
            ],
        },
        max_total=3,
        max_per_channel=2,
    )

    assert [post["message_id"] for post in result] == [2, 5, 1]
    assert ChannelReader._fair_distribute({}, max_total=5) == []


@pytest.mark.asyncio
async def test_fetch_posts_for_user_persists_filters_and_distributes() -> None:
    channels = [_channel(1, "one"), _channel(2, "two")]
    store = _FakeStore(
        subscriptions=[SimpleNamespace(channel=channel) for channel in channels],
        delivered={11},
    )
    userbot = _FakeUserbot(
        {
            "one": [
                {"message_id": 10, "date": "2026-01-02", "text": "a"},
                {"message_id": 11, "date": "2026-01-01", "text": "b"},
                {"message_id": 12, "date": "2026-01-03", "text": "c"},
            ],
            "two": [{"message_id": 20, "date": "2026-01-04", "text": "d"}],
        }
    )

    result = await _reader(store, userbot).fetch_posts_for_user(100, max_posts=3)

    assert [post["message_id"] for post in result] == [10, 20]
    assert result[0]["_channel_username"] == "one"
    assert len(store.persisted) == 2


@pytest.mark.asyncio
async def test_fetch_posts_for_user_records_channel_errors() -> None:
    channel = _channel(1, "bad")
    store = _FakeStore(subscriptions=[SimpleNamespace(channel=channel)])
    userbot = _FakeUserbot({"bad": RuntimeError("down")})

    result = await _reader(store, userbot).fetch_posts_for_user(100)

    assert result == []
    assert store.errors == 1


@pytest.mark.asyncio
async def test_fetch_posts_for_user_handles_removed_channel_404() -> None:
    channel = _channel(1, "removed")
    store = _FakeStore(subscriptions=[SimpleNamespace(channel=channel)])
    userbot = _FakeUserbot({"removed": ValueError("404 channel not found")})

    result = await _reader(store, userbot).fetch_posts_for_user(100)

    assert result == []
    assert store.errors == 1
    assert store.last_error == (channel, "fetch_failed", 3)
    assert store.persisted == []


@pytest.mark.asyncio
async def test_fetch_posts_for_channel_respects_disabled_and_unread_sorting() -> None:
    disabled = _channel(1, "disabled", active=False)
    store = _FakeStore(delivered={2})
    userbot = _FakeUserbot({"ok": []})

    assert await _reader(store, userbot).fetch_posts_for_channel(disabled, 100) == []

    channel = _channel(2, "ok")
    userbot.posts_by_username["ok"] = [
        {"message_id": 1, "date": "2026-01-01"},
        {"message_id": 3, "date": "2026-01-03"},
        {"message_id": 2, "date": "2026-01-04"},
    ]

    result = await _reader(store, userbot).fetch_posts_for_channel(channel, 100, max_posts=1)

    assert [post["message_id"] for post in result] == [3]
