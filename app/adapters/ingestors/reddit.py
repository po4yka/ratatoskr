"""Reddit source ingester using public subreddit JSON listings."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

import httpx

from app.adapters.ingestors._http import DEFAULT_MAX_RESPONSE_MB, fetch_json_capped
from app.application.ports.source_ingestors import (
    AuthSourceError,
    IngestedFeedItem,
    IngestedSource,
    RateLimitedSourceError,
    SourceFetchResult,
    TransientSourceError,
)
from app.core.url_utils import normalize_url

if TYPE_CHECKING:
    from collections.abc import Callable

_LISTINGS = {"hot", "new", "top", "rising"}


class RequestRateBudget:
    """Small in-process request budget for source pollers."""

    def __init__(
        self, *, max_requests_per_minute: int, now: Callable[[], float] = monotonic
    ) -> None:
        self.max_requests_per_minute = max(1, min(int(max_requests_per_minute), 100))
        self._now = now
        self._timestamps: list[float] = []

    def acquire(self) -> None:
        now = self._now()
        window_start = now - 60.0
        self._timestamps = [value for value in self._timestamps if value >= window_start]
        if len(self._timestamps) >= self.max_requests_per_minute:
            raise RateLimitedSourceError("Reddit request budget exhausted")
        self._timestamps.append(now)


class RedditIngester:
    """Poll one subreddit listing through Reddit's public JSON endpoint."""

    def __init__(
        self,
        *,
        subreddit: str,
        listing: str = "hot",
        limit: int = 25,
        enabled: bool = True,
        client: Any | None = None,
        rate_budget: RequestRateBudget | None = None,
        user_agent: str = "Ratatoskr/0.1 self-hosted signal ingester",
        base_url: str = "https://www.reddit.com",
        max_response_mb: int = DEFAULT_MAX_RESPONSE_MB,
    ) -> None:
        cleaned = subreddit.strip().removeprefix("r/").strip("/")
        if not cleaned:
            msg = "subreddit is required"
            raise ValueError(msg)
        listing_key = listing.strip().lower()
        if listing_key not in _LISTINGS:
            msg = f"Unsupported Reddit listing: {listing}"
            raise ValueError(msg)
        self.subreddit = cleaned
        self.listing = listing_key
        self.limit = max(1, min(int(limit), 100))
        self.enabled = enabled
        self.client = client or httpx.AsyncClient(timeout=20.0)
        self.rate_budget = rate_budget or RequestRateBudget(max_requests_per_minute=60)
        self.user_agent = user_agent
        self.base_url = base_url.rstrip("/")
        self._max_response_bytes = max_response_mb * 1024 * 1024
        self.name = f"reddit:{self.subreddit}:{self.listing}"

    def is_enabled(self) -> bool:
        return self.enabled

    def source_identity(self) -> IngestedSource:
        return IngestedSource(
            kind="reddit",
            external_id=f"reddit:{self.subreddit}:{self.listing}",
            url=f"https://www.reddit.com/r/{self.subreddit}/{self.listing}/",
            title=f"r/{self.subreddit} {self.listing}",
            metadata={"listing": self.listing, "free_tier_guard": "public-json"},
        )

    async def fetch(self) -> SourceFetchResult:
        self.rate_budget.acquire()
        url = f"{self.base_url}/r/{self.subreddit}/{self.listing}.json?limit={self.limit}"
        payload = await fetch_json_capped(
            self.client,
            url,
            max_bytes=self._max_response_bytes,
            provider="Reddit",
            headers={"User-Agent": self.user_agent},
            check_status=self._check_status,
        )
        children = (
            ((payload.get("data") or {}).get("children") or []) if isinstance(payload, dict) else []
        )
        items = [
            self._normalize_child(child.get("data") or {})
            for child in children
            if isinstance(child, dict) and isinstance(child.get("data"), dict)
        ]
        return SourceFetchResult(
            source=self.source_identity(),
            items=items,
        )

    @staticmethod
    def _check_status(response: Any) -> None:
        if response.status_code == 429:
            raise RateLimitedSourceError("Reddit API returned 429")
        if response.status_code in {401, 403}:
            raise AuthSourceError(f"Reddit API denied access: {response.status_code}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TransientSourceError(f"Reddit API error: {response.status_code}") from exc

    def _normalize_child(self, raw: dict[str, Any]) -> IngestedFeedItem:
        post_id = str(raw.get("id") or raw.get("name") or "").strip()
        if not post_id:
            msg = "Reddit post missing id"
            raise TransientSourceError(msg)
        permalink = str(raw.get("permalink") or "")
        comments_url = (
            f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
        )
        outbound_url = raw.get("url_overridden_by_dest") or raw.get("url") or comments_url
        try:
            canonical_url = normalize_url(str(outbound_url))
        except ValueError:
            canonical_url = str(outbound_url) if outbound_url else comments_url
        created = raw.get("created_utc")
        published_at = datetime.fromtimestamp(int(created), tz=UTC) if created is not None else None
        score = raw.get("score")
        comments = raw.get("num_comments")
        return IngestedFeedItem(
            external_id=f"reddit:{post_id}",
            canonical_url=canonical_url,
            title=raw.get("title"),
            content_text=raw.get("selftext") or None,
            author=raw.get("author"),
            published_at=published_at,
            engagement={
                "score": float(score) if score is not None else None,
                "comments": int(comments) if comments is not None else None,
            },
            metadata={
                "provider": "reddit",
                "subreddit": self.subreddit,
                "listing": self.listing,
                "permalink": comments_url,
                "is_self": bool(raw.get("is_self")),
                "over_18": bool(raw.get("over_18")),
            },
        )
