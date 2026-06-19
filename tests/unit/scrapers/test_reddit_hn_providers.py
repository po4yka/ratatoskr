from __future__ import annotations

from typing import Any

import pytest

from app.adapters.content.scraper.factory import SCRAPER_PROVIDER_DESCRIPTOR_BY_NAME
from app.adapters.content.scraper.hn_provider import HackerNewsProvider
from app.adapters.content.scraper.reddit_provider import RedditProvider
from app.config.scraper import (
    DEFAULT_SCRAPER_PROVIDER_ORDER,
    SCRAPER_PROVIDER_TOKENS,
    ScraperConfig,
)
from app.core.call_status import CallStatus

pytestmark = pytest.mark.no_network


class _FakeResponse:
    def __init__(self, payload: Any, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.closed = False

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append((url, headers))
        return _FakeResponse(self._payload)

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_reddit_provider_extracts_submission_and_top_five_comments() -> None:
    payload = [
        {
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "abc123",
                            "title": "Python discussion",
                            "subreddit": "python",
                            "author": "op_user",
                            "selftext": "Original post body with useful context.",
                            "score": 42,
                            "num_comments": 6,
                        },
                    }
                ]
            }
        },
        {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {"author": f"user_{idx}", "body": f"Reply body {idx}", "score": idx},
                    }
                    for idx in range(1, 7)
                ]
            }
        },
    ]
    client = _FakeClient(payload)
    provider = RedditProvider(user_agent="Ratatoskr tests", top_comments=5, client=client)

    result = await provider.scrape_markdown(
        "https://www.reddit.com/r/python/comments/abc123/python_discussion/"
    )

    assert result.status == CallStatus.OK
    assert result.endpoint == "reddit"
    assert "# Python discussion" in (result.content_markdown or "")
    assert "Original post body with useful context." in (result.content_markdown or "")
    assert "Reply body 5" in (result.content_markdown or "")
    assert "Reply body 6" not in (result.content_markdown or "")
    assert client.calls[0][0].startswith("https://www.reddit.com/comments/abc123.json")
    assert client.calls[0][1] == {"Accept": "application/json", "User-Agent": "Ratatoskr tests"}


def test_reddit_provider_only_supports_reddit_comment_urls() -> None:
    provider = RedditProvider(user_agent="Ratatoskr tests")

    assert provider.supports_url("https://old.reddit.com/r/selfhosted/comments/abc123/title/")
    assert provider.supports_url("https://redd.it/abc123")
    assert not provider.supports_url("https://www.reddit.com/r/selfhosted/")
    assert not provider.supports_url("https://example.com/r/selfhosted/comments/abc123/title/")


@pytest.mark.asyncio
async def test_hn_provider_extracts_story_and_comment_tree() -> None:
    payload = {
        "id": 12345,
        "title": "Launch HN: Ratatoskr",
        "url": "https://example.com/ratatoskr",
        "author": "founder",
        "points": 128,
        "created_at": "2026-06-19T10:00:00Z",
        "text": "<p>Story text with <b>HTML</b>.</p>",
        "children": [
            {
                "id": 1,
                "author": "alice",
                "text": "<p>First comment.</p>",
                "children": [
                    {"id": 2, "author": "bob", "text": "<p>Nested comment.</p>", "children": []}
                ],
            },
            {"id": 3, "author": "carol", "text": "<p>Second top-level comment.</p>", "children": []},
        ],
    }
    client = _FakeClient(payload)
    provider = HackerNewsProvider(top_comments=2, client=client)

    result = await provider.scrape_markdown("https://news.ycombinator.com/item?id=12345")

    assert result.status == CallStatus.OK
    assert result.endpoint == "hn"
    assert "# Launch HN: Ratatoskr" in (result.content_markdown or "")
    assert "Story text with HTML." in (result.content_markdown or "")
    assert "First comment." in (result.content_markdown or "")
    assert "Nested comment." in (result.content_markdown or "")
    assert "Second top-level comment." not in (result.content_markdown or "")
    assert client.calls == [
        ("https://hn.algolia.com/api/v1/items/12345", {"Accept": "application/json"})
    ]


def test_hn_provider_only_supports_hn_item_urls() -> None:
    provider = HackerNewsProvider()

    assert provider.supports_url("https://news.ycombinator.com/item?id=12345")
    assert provider.supports_url("https://hn.algolia.com/api/v1/items/12345")
    assert not provider.supports_url("https://news.ycombinator.com/news")
    assert not provider.supports_url("https://example.com/item?id=12345")


def test_scraper_config_registers_reddit_and_hn_before_generic_providers() -> None:
    config = ScraperConfig()

    assert {"reddit", "hn"}.issubset(SCRAPER_PROVIDER_TOKENS)
    assert DEFAULT_SCRAPER_PROVIDER_ORDER[:3] == ["reddit", "hn", "scrapling"]
    assert config.provider_order[:3] == ["reddit", "hn", "scrapling"]
    assert {"reddit", "hn"}.issubset(SCRAPER_PROVIDER_DESCRIPTOR_BY_NAME)


def test_scraper_config_accepts_reddit_and_hn_force_provider_tokens() -> None:
    assert ScraperConfig(SCRAPER_FORCE_PROVIDER="reddit").force_provider == "reddit"
    assert ScraperConfig(SCRAPER_FORCE_PROVIDER="hn").force_provider == "hn"
