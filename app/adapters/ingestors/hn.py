"""Hacker News source ingester."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from app.application.ports.source_ingestors import (
    IngestedFeedItem,
    IngestedSource,
    RateLimitedSourceError,
    SourceFetchResult,
    TransientSourceError,
)
from app.core.logging_utils import get_logger
from app.core.url_utils import normalize_url

logger = get_logger(__name__)

_FEEDS = {
    "top": "topstories",
    "best": "beststories",
    "new": "newstories",
    "newest": "newstories",
}


def _representative_error(errors: list[Exception]) -> Exception:
    """Pick the error that best represents a total item-fetch failure.

    Prefer a rate-limit error so the runner honors its retry_at/backoff; fall
    back to the first error otherwise.
    """
    for error in errors:
        if isinstance(error, RateLimitedSourceError):
            return error
    return errors[0]


class HackerNewsIngester:
    """Poll one Hacker News listing through the official Firebase API."""

    def __init__(
        self,
        *,
        feed: str = "top",
        limit: int = 30,
        enabled: bool = True,
        client: Any | None = None,
        base_url: str = "https://hacker-news.firebaseio.com/v0",
        max_concurrency: int = 5,
    ) -> None:
        key = feed.strip().lower()
        if key not in _FEEDS:
            msg = f"Unsupported Hacker News feed: {feed}"
            raise ValueError(msg)
        self.feed = key
        self.limit = max(1, min(int(limit), 100))
        self.enabled = enabled
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self.base_url = base_url.rstrip("/")
        self.max_concurrency = max(1, int(max_concurrency))
        self.name = f"hacker_news:{self.feed}"

    def is_enabled(self) -> bool:
        return self.enabled

    def source_identity(self) -> IngestedSource:
        return IngestedSource(
            kind="hacker_news",
            external_id=f"hn:{self.feed}",
            url=f"https://news.ycombinator.com/{self.feed if self.feed != 'newest' else 'new'}",
            title=f"Hacker News {self.feed}",
            metadata={"api": "firebase", "feed": self.feed},
        )

    async def fetch(self) -> SourceFetchResult:
        ids = await self._get_json(f"{self.base_url}/{_FEEDS[self.feed]}.json")
        if not isinstance(ids, list):
            raise TransientSourceError("Hacker News listing response was not a list")

        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def _load(item_id: Any) -> IngestedFeedItem | None:
            async with semaphore:
                raw = await self._get_json(f"{self.base_url}/item/{int(item_id)}.json")
            if not isinstance(raw, dict) or raw.get("type") != "story" or raw.get("deleted"):
                return None
            return self._normalize_item(raw)

        # Fan out the per-item lookups concurrently (bounded by the semaphore).
        # return_exceptions=True so one failing item fetch never discards the
        # whole batch: individual failures are skipped and the items that did
        # load are still returned. gather preserves listing order.
        results = await asyncio.gather(
            *(_load(item_id) for item_id in ids[: self.limit]),
            return_exceptions=True,
        )

        items: list[IngestedFeedItem] = []
        errors: list[Exception] = []
        for result in results:
            if isinstance(result, IngestedFeedItem):
                items.append(result)
            elif isinstance(result, Exception):
                errors.append(result)
            elif isinstance(result, BaseException):
                # CancelledError / KeyboardInterrupt / SystemExit must propagate,
                # never be swallowed as a skippable item failure.
                raise result
            # None => filtered non-story/deleted item; skip silently.

        if errors and not items:
            # Every item fetch failed: surface the failure so the runner backs
            # off instead of recording a false success (which would clear the
            # backoff and tight-loop during an outage).
            raise _representative_error(errors)

        if errors:
            logger.warning(
                "hacker_news_partial_item_fetch",
                extra={"feed": self.feed, "fetched": len(items), "dropped": len(errors)},
            )

        return SourceFetchResult(
            source=self.source_identity(),
            items=items,
        )

    async def _get_json(self, url: str) -> Any:
        response = await self.client.get(url)
        if response.status_code == 429:
            raise RateLimitedSourceError("Hacker News API returned 429")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TransientSourceError(f"Hacker News API error: {response.status_code}") from exc
        return response.json()

    @staticmethod
    def _normalize_item(raw: dict[str, Any]) -> IngestedFeedItem:
        item_id = int(raw["id"])
        url = raw.get("url") or f"https://news.ycombinator.com/item?id={item_id}"
        try:
            canonical_url = normalize_url(str(url))
        except ValueError:
            canonical_url = str(url)
        score = raw.get("score")
        comments = raw.get("descendants")
        timestamp = raw.get("time")
        published_at = (
            datetime.fromtimestamp(int(timestamp), tz=UTC) if timestamp is not None else None
        )
        return IngestedFeedItem(
            external_id=f"hn:{item_id}",
            canonical_url=canonical_url,
            title=raw.get("title"),
            author=raw.get("by"),
            published_at=published_at,
            engagement={
                "score": float(score) if score is not None else None,
                "comments": int(comments) if comments is not None else None,
            },
            metadata={
                "provider": "hacker_news",
                "hn_id": item_id,
                "hn_comments_url": f"https://news.ycombinator.com/item?id={item_id}",
            },
        )
