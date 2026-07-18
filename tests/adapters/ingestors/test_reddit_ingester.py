from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.adapters.ingestors.reddit import RedditIngester, RequestRateBudget
from app.application.ports.source_ingestors import (
    AuthSourceError,
    RateLimitedSourceError,
    TransientSourceError,
)


class _StreamCM:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __aenter__(self) -> httpx.Response:
        return self._response

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeClient:
    def __init__(
        self,
        response: object,
        *,
        status_code: int = 200,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        self.response = response
        self.status_code = status_code
        self.response_headers = response_headers
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def stream(self, method: str, url: str, *, headers: dict[str, str] | None = None) -> _StreamCM:
        self.urls.append(url)
        self.headers.append(headers or {})
        return _StreamCM(
            httpx.Response(
                self.status_code,
                json=self.response if self.status_code < 400 else None,
                headers=self.response_headers,
                request=httpx.Request("GET", url),
            )
        )


@pytest.mark.asyncio
async def test_reddit_ingester_normalizes_subreddit_listing() -> None:
    client = _FakeClient(
        {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "abc123",
                            "title": "Useful post",
                            "author": "alice",
                            "score": 98,
                            "num_comments": 7,
                            "created_utc": 1_777_500_000,
                            "permalink": "/r/selfhosted/comments/abc123/useful_post/",
                            "url": "https://example.com/post?utm_medium=social",
                            "selftext": "discussion body",
                        }
                    }
                ]
            }
        }
    )
    ingester = RedditIngester(subreddit="selfhosted", listing="hot", limit=1, client=client)

    result = await ingester.fetch()

    assert result.source.kind == "reddit"
    assert result.source.external_id == "reddit:selfhosted:hot"
    assert result.items[0].external_id == "reddit:abc123"
    assert result.items[0].canonical_url == "https://example.com/post"
    assert result.items[0].author == "alice"
    assert result.items[0].published_at == dt.datetime.fromtimestamp(1_777_500_000, tz=dt.UTC)
    assert result.items[0].engagement == {"score": 98.0, "comments": 7}
    assert "Ratatoskr" in client.headers[0]["User-Agent"]


@pytest.mark.asyncio
async def test_reddit_ingester_maps_rate_limit_and_auth_errors() -> None:
    with pytest.raises(RateLimitedSourceError):
        await RedditIngester(
            subreddit="python",
            client=_FakeClient({}, status_code=429),
        ).fetch()

    with pytest.raises(AuthSourceError):
        await RedditIngester(
            subreddit="private",
            client=_FakeClient({}, status_code=403),
        ).fetch()


@pytest.mark.asyncio
async def test_reddit_ingester_rejects_oversized_response() -> None:
    client = _FakeClient(
        {"data": {"children": []}},
        response_headers={"content-length": str(50 * 1024 * 1024)},
    )
    with pytest.raises(TransientSourceError):
        await RedditIngester(subreddit="python", client=client, max_response_mb=10).fetch()


@pytest.mark.asyncio
async def test_reddit_ingester_enforces_request_budget_before_http_call() -> None:
    client = _FakeClient({"data": {"children": []}})
    budget = RequestRateBudget(max_requests_per_minute=1, now=lambda: 100.0)
    ingester = RedditIngester(subreddit="python", client=client, rate_budget=budget)

    await ingester.fetch()
    with pytest.raises(RateLimitedSourceError):
        await ingester.fetch()

    assert len(client.urls) == 1
