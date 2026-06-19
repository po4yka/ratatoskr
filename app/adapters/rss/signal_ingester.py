"""RSS/Substack source ingester implementation."""

from __future__ import annotations

import asyncio
from typing import Any

from app.adapters.rss.feed_fetcher import FeedResult, fetch_feed
from app.adapters.rss.substack import is_substack_url
from app.application.ports.source_ingestors import (
    IngestedFeedItem,
    IngestedSource,
    SourceFetchResult,
)


class RssSignalIngester:
    """Adapt one legacy RSS feed row to the generic source ingester contract."""

    def __init__(self, feed: dict[str, Any], *, fetcher=fetch_feed) -> None:
        self.feed = feed
        self.fetcher = fetcher
        self.name = f"rss:{feed.get('url')}"

    def is_enabled(self) -> bool:
        return True

    def source_identity(self) -> IngestedSource:
        url = str(self.feed.get("url") or "")
        source_kind = "substack" if is_substack_url(url) else "rss"
        return IngestedSource(
            kind=source_kind,
            external_id=url,
            url=url,
            title=self.feed.get("title"),
            description=self.feed.get("description"),
            site_url=self.feed.get("site_url"),
            metadata={
                "etag": self.feed.get("etag"),
                "last_modified": self.feed.get("last_modified"),
                "legacy_rss_feed_id": self.feed.get("id"),
            },
        )

    async def fetch(self) -> SourceFetchResult:
        url = str(self.feed.get("url") or "")
        etag = self.feed.get("etag")
        last_modified = self.feed.get("last_modified")
        result = await asyncio.to_thread(
            self.fetcher,
            url,
            etag=etag,
            last_modified=last_modified,
        )
        return self.normalize_result(result)

    def normalize_result(self, result: FeedResult) -> SourceFetchResult:
        metadata = {
            "etag": result.etag or self.feed.get("etag"),
            "last_modified": result.last_modified or self.feed.get("last_modified"),
            "legacy_rss_feed_id": self.feed.get("id"),
        }
        identity = self.source_identity()
        return SourceFetchResult(
            source=IngestedSource(
                kind=identity.kind,
                external_id=identity.external_id,
                url=identity.url,
                title=result.title or self.feed.get("title"),
                description=result.description or self.feed.get("description"),
                site_url=result.site_url or self.feed.get("site_url"),
                metadata=metadata,
            ),
            items=[
                IngestedFeedItem(
                    external_id=entry.guid,
                    canonical_url=entry.url,
                    title=entry.title,
                    content_text=entry.content,
                    author=entry.author,
                    published_at=entry.published_at,
                    metadata={"legacy_source": "rss"},
                )
                for entry in result.entries
            ],
            not_modified=result.not_modified,
            metadata=metadata,
        )
