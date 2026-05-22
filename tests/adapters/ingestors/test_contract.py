from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, cast

import pytest

from app.adapters.ingestors.runner import SourceIngestionRunner
from app.application.ports.source_ingestors import (
    IngestedFeedItem,
    IngestedSource,
    SourceFetchResult,
    SourceIngester,
)
from app.core.time_utils import UTC

if TYPE_CHECKING:
    from app.application.ports.signal_sources import SignalSourceRepositoryPort


class _FakeRepository:
    def __init__(self) -> None:
        self.sources: list[dict] = []
        self.items: list[dict] = []
        self.subscriptions: list[dict] = []
        self.successes: list[int] = []
        self.errors: list[dict] = []
        self.run_state: dict | None = {
            "is_active": True,
            "active_subscription": True,
            "backoff_until": None,
        }

    async def async_upsert_source(self, **kwargs):
        self.sources.append(kwargs)
        return {"id": 1, **kwargs}

    async def async_upsert_feed_item(self, **kwargs):
        self.items.append(kwargs)
        return {"id": len(self.items), **kwargs}

    async def async_subscribe(self, **kwargs):
        self.subscriptions.append(kwargs)
        return {"id": len(self.subscriptions), **kwargs}

    async def async_record_source_fetch_success(self, source_id: int):
        self.successes.append(source_id)

    async def async_record_source_fetch_error(self, **kwargs):
        self.errors.append(kwargs)
        return False

    async def async_get_source_run_state(self, source_id: int):
        return self.run_state


class _FakeIngester:
    name = "fake"

    def __init__(self) -> None:
        self.fetches = 0

    def is_enabled(self) -> bool:
        return True

    def source_identity(self) -> IngestedSource:
        return IngestedSource(
            kind="fake",
            external_id="fake:one",
            url="https://example.test/feed",
            title="Fake",
            metadata={"source": "test"},
        )

    async def fetch(self) -> SourceFetchResult:
        self.fetches += 1
        return SourceFetchResult(
            source=self.source_identity(),
            items=[
                IngestedFeedItem(
                    external_id="item-1",
                    canonical_url="https://example.test/item",
                    title="Item",
                    content_text="Body",
                    author="Author",
                    published_at=dt.datetime(2026, 4, 30, tzinfo=UTC),
                    engagement={"comments": 3, "score": 4.0},
                    metadata={"raw": True},
                )
            ],
        )


def test_source_ingester_protocol_is_runtime_checkable() -> None:
    assert isinstance(_FakeIngester(), SourceIngester)


@pytest.mark.asyncio
async def test_runner_persists_normalized_items_and_subscriptions() -> None:
    repo = _FakeRepository()
    runner = SourceIngestionRunner(
        repository=cast("SignalSourceRepositoryPort", repo),
        ingesters=[_FakeIngester()],
        subscriber_user_ids=[1001],
    )

    stats = await runner.run_once()

    assert stats == {"enabled": 1, "sources": 1, "items": 1, "errors": 0, "skipped": 0}
    assert repo.sources[0]["kind"] == "fake"
    assert repo.items[0]["external_id"] == "item-1"
    assert repo.items[0]["engagement"]["comments"] == 3
    assert repo.subscriptions == [{"user_id": 1001, "source_id": 1}]
    assert repo.successes == [1]


@pytest.mark.asyncio
async def test_runner_skips_disabled_persisted_source_without_fetching() -> None:
    repo = _FakeRepository()
    repo.run_state = {"is_active": False, "active_subscription": True, "backoff_until": None}
    ingester = _FakeIngester()
    runner = SourceIngestionRunner(
        repository=cast("SignalSourceRepositoryPort", repo),
        ingesters=[ingester],
        subscriber_user_ids=[1001],
    )

    stats = await runner.run_once()

    assert stats == {"enabled": 1, "sources": 0, "items": 0, "errors": 0, "skipped": 1}
    assert ingester.fetches == 0


@pytest.mark.asyncio
async def test_runner_skips_source_until_backoff_expires() -> None:
    repo = _FakeRepository()
    repo.run_state = {
        "is_active": True,
        "active_subscription": True,
        "backoff_until": dt.datetime(2027, 5, 22, tzinfo=UTC),
    }
    ingester = _FakeIngester()
    runner = SourceIngestionRunner(
        repository=cast("SignalSourceRepositoryPort", repo),
        ingesters=[ingester],
        subscriber_user_ids=[1001],
    )

    stats = await runner.run_once()

    assert stats == {"enabled": 1, "sources": 0, "items": 0, "errors": 0, "skipped": 1}
    assert ingester.fetches == 0


@pytest.mark.asyncio
async def test_runner_limits_items_by_source_control() -> None:
    repo = _FakeRepository()
    repo.run_state = {
        "is_active": True,
        "active_subscription": True,
        "backoff_until": None,
        "max_items_per_run": 1,
    }
    runner = SourceIngestionRunner(
        repository=cast("SignalSourceRepositoryPort", repo),
        ingesters=[_FakeIngester()],
        subscriber_user_ids=[1001],
    )

    stats = await runner.run_once()

    assert stats == {"enabled": 1, "sources": 1, "items": 1, "errors": 0, "skipped": 0}
