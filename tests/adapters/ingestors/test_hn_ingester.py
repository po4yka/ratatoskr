from __future__ import annotations

import asyncio
import datetime as dt

import httpx
import pytest

from app.adapters.ingestors.hn import HackerNewsIngester
from app.application.ports.source_ingestors import (
    RateLimitedSourceError,
    TransientSourceError,
)

_BASE = "https://hacker-news.firebaseio.com/v0"


def _story(item_id: int) -> dict[str, object]:
    return {"id": item_id, "type": "story", "title": f"s{item_id}", "time": 1_777_500_000}


class _FakeClient:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    async def get(self, url: str, **_kwargs):
        self.urls.append(url)
        payload = self.responses[url]
        if isinstance(payload, int):
            return httpx.Response(payload, request=httpx.Request("GET", url))
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))


@pytest.mark.asyncio
async def test_hn_ingester_normalizes_items_with_engagement() -> None:
    client = _FakeClient(
        {
            "https://hacker-news.firebaseio.com/v0/topstories.json": [42],
            "https://hacker-news.firebaseio.com/v0/item/42.json": {
                "id": 42,
                "type": "story",
                "title": "Launch",
                "url": "https://example.com/launch?utm_source=hn",
                "by": "pg",
                "score": 123,
                "descendants": 45,
                "time": 1_777_500_000,
            },
        }
    )
    ingester = HackerNewsIngester(feed="top", limit=1, client=client)

    result = await ingester.fetch()

    assert result.source.kind == "hacker_news"
    assert result.source.external_id == "hn:top"
    assert result.items[0].external_id == "hn:42"
    assert result.items[0].canonical_url == "https://example.com/launch"
    assert result.items[0].author == "pg"
    assert result.items[0].published_at == dt.datetime.fromtimestamp(1_777_500_000, tz=dt.UTC)
    assert result.items[0].engagement == {"score": 123.0, "comments": 45}


@pytest.mark.asyncio
async def test_hn_ingester_keeps_items_when_one_item_fetch_fails() -> None:
    # Listing has 3 stories; the middle item 500s. Previously any single item
    # failure discarded the entire batch -- now the failure is skipped and the
    # items that loaded survive.
    client = _FakeClient(
        {
            f"{_BASE}/topstories.json": [1, 2, 3],
            f"{_BASE}/item/1.json": _story(1),
            f"{_BASE}/item/2.json": 500,
            f"{_BASE}/item/3.json": _story(3),
        }
    )
    ingester = HackerNewsIngester(feed="top", limit=3, client=client, max_concurrency=5)

    result = await ingester.fetch()

    assert [item.external_id for item in result.items] == ["hn:1", "hn:3"]


@pytest.mark.asyncio
async def test_hn_ingester_raises_when_all_item_fetches_fail() -> None:
    # With no items to return, surface the failure so the runner backs off
    # rather than recording a false success.
    client = _FakeClient(
        {
            f"{_BASE}/topstories.json": [1, 2],
            f"{_BASE}/item/1.json": 500,
            f"{_BASE}/item/2.json": 500,
        }
    )
    ingester = HackerNewsIngester(feed="top", limit=2, client=client)

    with pytest.raises(TransientSourceError):
        await ingester.fetch()


@pytest.mark.asyncio
async def test_hn_ingester_all_failed_prefers_rate_limit_error() -> None:
    # A rate-limit error among the failures wins so its retry_at/backoff is honored.
    client = _FakeClient(
        {
            f"{_BASE}/topstories.json": [1, 2],
            f"{_BASE}/item/1.json": 500,
            f"{_BASE}/item/2.json": 429,
        }
    )
    ingester = HackerNewsIngester(feed="top", limit=2, client=client)

    with pytest.raises(RateLimitedSourceError):
        await ingester.fetch()


@pytest.mark.asyncio
async def test_hn_ingester_turns_429_into_rate_limit_error() -> None:
    client = _FakeClient({"https://hacker-news.firebaseio.com/v0/newstories.json": 429})
    ingester = HackerNewsIngester(feed="new", client=client)

    with pytest.raises(RateLimitedSourceError):
        await ingester.fetch()


class _ConcurrencyTrackingClient:
    """Async client that records the peak number of in-flight item requests."""

    def __init__(self, *, item_count: int) -> None:
        self.item_count = item_count
        self.in_flight = 0
        self.max_in_flight = 0
        self._gate = asyncio.Event()

    async def get(self, url: str, **_kwargs):
        if url.endswith("topstories.json"):
            return httpx.Response(
                200, json=list(range(self.item_count)), request=httpx.Request("GET", url)
            )
        # Item request: hold all concurrent calls open until the gate releases so the
        # peak in-flight count reflects real overlap, not lucky scheduling.
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.in_flight >= min(self.item_count, 5):
            self._gate.set()
        await self._gate.wait()
        self.in_flight -= 1
        item_id = int(url.rsplit("/", 1)[1].removesuffix(".json"))
        return httpx.Response(
            200,
            json={"id": item_id, "type": "story", "title": f"s{item_id}", "time": 1_777_500_000},
            request=httpx.Request("GET", url),
        )


@pytest.mark.asyncio
async def test_hn_ingester_fetches_items_concurrently() -> None:
    client = _ConcurrencyTrackingClient(item_count=10)
    ingester = HackerNewsIngester(feed="top", limit=10, client=client, max_concurrency=5)

    # A concurrent ticker proves the event loop is not blocked during the fetch.
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        for _ in range(3):
            await asyncio.sleep(0)
            ticks += 1

    result, _ = await asyncio.gather(ingester.fetch(), _ticker())

    assert len(result.items) == 10
    assert client.max_in_flight > 1  # item lookups overlapped (not serialized)
    assert client.max_in_flight <= 5  # but stayed within the concurrency bound
    assert ticks == 3  # loop kept running alongside the fetch
