"""RSS poller integration with generic signal sources."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.adapters.rss.feed_fetcher import FeedEntry, FeedResult
from app.core.time_utils import UTC


class _FakeRSSRepo:
    instance = None

    def __init__(self, _db) -> None:
        self.items: list[dict] = []
        self.feed_errors: list[dict] = []
        self.delivery_target_queries: list[list[int]] = []
        self.list_active_feeds_limits: list[int | None] = []
        _FakeRSSRepo.instance = self

    async def async_list_active_feeds(self, *, limit: int | None = None):
        self.list_active_feeds_limits.append(limit)
        return [
            {
                "id": 10,
                "url": "https://example.com/feed.xml",
                "title": "Example",
                "description": "Feed",
                "site_url": "https://example.com",
                "etag": None,
                "last_modified": None,
            }
        ]

    async def async_create_feed_item(self, **kwargs):
        self.items.append(kwargs)
        return {"id": 100, **kwargs}

    async def async_create_feed_items(self, *, feed_id, items):
        self.items.extend({"feed_id": feed_id, **item} for item in items)
        return [
            {"id": 100 + index, "feed": feed_id, "feed_id": feed_id, **item}
            for index, item in enumerate(items)
        ]

    async def async_list_delivery_targets(self, new_item_ids):
        self.delivery_target_queries.append(new_item_ids)
        return [{"id": 100, "subscriber_ids": [1001]}]

    async def async_update_feed_fetch_success(self, **kwargs):
        return None

    async def async_record_feed_fetch_error(self, **kwargs):
        self.feed_errors.append(kwargs)


class _FakeSignalRepo:
    instance = None
    default_run_state = {
        "is_active": True,
        "backoff_until": None,
        "max_items_per_run": None,
    }

    def __init__(self, _db) -> None:
        self.sources: list[dict] = []
        self.items: list[dict] = []
        self.subscriptions: list[dict] = []
        self.bulk_subscriptions: list[dict] = []
        self.successes: list[int] = []
        self.errors: list[dict] = []
        self.run_state: dict | None = dict(_FakeSignalRepo.default_run_state)
        _FakeSignalRepo.instance = self

    async def async_upsert_source(self, **kwargs):
        self.sources.append(kwargs)
        return {"id": 200, **kwargs}

    async def async_upsert_feed_item(self, **kwargs):
        self.items.append(kwargs)
        return {"id": 300, **kwargs}

    async def async_upsert_feed_items(self, *, source_id, items):
        self.items.extend({"source_id": source_id, **item} for item in items)
        return [
            {"id": 300 + index, "source_id": source_id, **item} for index, item in enumerate(items)
        ]

    async def async_subscribe(self, **kwargs):
        self.subscriptions.append(kwargs)
        return {"id": 400, **kwargs}

    async def async_subscribe_many(self, **kwargs):
        self.bulk_subscriptions.append(kwargs)
        for user_id in kwargs["user_ids"]:
            self.subscriptions.append({"user_id": user_id, "source_id": kwargs["source_id"]})

    async def async_record_source_fetch_success(self, source_id: int):
        self.successes.append(source_id)

    async def async_record_source_fetch_error(self, **kwargs):
        self.errors.append(kwargs)
        return False

    async def async_get_source_run_state(self, source_id: int):
        return self.run_state


@pytest.mark.asyncio
async def test_rss_poll_mirrors_new_items_into_signal_sources(monkeypatch):
    from app.adapters.rss import feed_poller

    monkeypatch.setattr(feed_poller, "RSSFeedRepositoryAdapter", _FakeRSSRepo)
    monkeypatch.setattr(feed_poller, "SignalSourceRepositoryAdapter", _FakeSignalRepo)
    monkeypatch.setattr(
        feed_poller,
        "fetch_feed",
        lambda *_args, **_kwargs: FeedResult(
            title="Example",
            description="Feed",
            site_url="https://example.com",
            entries=[
                FeedEntry(
                    guid="guid-1",
                    title="Item",
                    url="https://example.com/item",
                    content="body",
                    author="Author",
                    published_at=dt.datetime(2026, 4, 30, tzinfo=UTC),
                )
            ],
        ),
    )

    stats = await feed_poller.poll_all_feeds(SimpleNamespace())

    rss_repo = _FakeRSSRepo.instance
    signal_repo = _FakeSignalRepo.instance
    assert stats["new_item_ids"] == [100]
    assert rss_repo.delivery_target_queries == [[100]]
    assert signal_repo.sources[0]["kind"] == "rss"
    assert signal_repo.items[0]["external_id"] == "guid-1"
    assert signal_repo.bulk_subscriptions == [{"source_id": 200, "user_ids": [1001]}]
    assert signal_repo.subscriptions == [{"user_id": 1001, "source_id": 200}]
    assert signal_repo.successes == [200]


@pytest.mark.asyncio
async def test_rss_poll_passes_feed_limit_to_repository(monkeypatch):
    from app.adapters.rss import feed_poller

    monkeypatch.setattr(feed_poller, "RSSFeedRepositoryAdapter", _FakeRSSRepo)
    monkeypatch.setattr(feed_poller, "SignalSourceRepositoryAdapter", _FakeSignalRepo)
    monkeypatch.setattr(
        feed_poller, "fetch_feed", lambda *_a, **_k: FeedResult(title="Example")
    )

    await feed_poller.poll_all_feeds(SimpleNamespace(), limit=25)

    assert _FakeRSSRepo.instance.list_active_feeds_limits == [25]


@pytest.mark.asyncio
async def test_rss_poll_skips_disabled_signal_source_without_fetch(monkeypatch):
    from app.adapters.rss import feed_poller

    monkeypatch.setattr(feed_poller, "RSSFeedRepositoryAdapter", _FakeRSSRepo)
    monkeypatch.setattr(feed_poller, "SignalSourceRepositoryAdapter", _FakeSignalRepo)
    fetches = {"count": 0}

    def _fetch(*_args, **_kwargs):
        fetches["count"] += 1
        return FeedResult(title="Example")

    monkeypatch.setattr(feed_poller, "fetch_feed", _fetch)

    _FakeSignalRepo.default_run_state = {"is_active": False, "backoff_until": None}
    try:
        stats = await feed_poller.poll_all_feeds(SimpleNamespace())
    finally:
        _FakeSignalRepo.default_run_state = {
            "is_active": True,
            "backoff_until": None,
            "max_items_per_run": None,
        }

    assert stats["skipped"] == 1
    assert fetches["count"] == 0


@pytest.mark.asyncio
async def test_rss_poll_records_signal_source_error_for_broken_feed(monkeypatch):
    from app.adapters.rss import feed_poller

    monkeypatch.setattr(feed_poller, "RSSFeedRepositoryAdapter", _FakeRSSRepo)
    monkeypatch.setattr(feed_poller, "SignalSourceRepositoryAdapter", _FakeSignalRepo)

    def _broken_fetch(*_args, **_kwargs):
        raise RuntimeError("feed is broken")

    monkeypatch.setattr(feed_poller, "fetch_feed", _broken_fetch)

    stats = await feed_poller.poll_all_feeds(SimpleNamespace())

    rss_repo = _FakeRSSRepo.instance
    signal_repo = _FakeSignalRepo.instance
    assert stats["errors"] == 1
    assert rss_repo.feed_errors[0]["error"] == "feed is broken"
    assert signal_repo.sources[0]["kind"] == "rss"
    assert signal_repo.errors[0]["source_id"] == 200
    assert signal_repo.errors[0]["max_errors"] == feed_poller.MAX_FETCH_ERRORS
