"""RSS feed polling service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.adapters.rss.feed_fetcher import fetch_feed
from app.adapters.rss.signal_ingester import RssSignalIngester
from app.adapters.rss.substack import is_substack_url
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.infrastructure.persistence.repositories.rss_feed_repository import (
    RSSFeedRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.signal_source_repository import (
    SignalSourceRepositoryAdapter,
)

if TYPE_CHECKING:
    from app.db.session import Database

logger = get_logger(__name__)

MAX_FETCH_ERRORS = 10
SIGNAL_SOURCE_BASE_BACKOFF_SECONDS = 300


@dataclass(slots=True)
class _FeedPollResult:
    polled: int = 0
    new_items: int = 0
    errors: int = 0
    skipped: int = 0
    new_item_ids: list[int] = field(default_factory=list)


def _feed_item_payload(item_result: Any) -> dict[str, Any]:
    return {
        "guid": item_result.external_id,
        "title": item_result.title,
        "url": item_result.canonical_url,
        "content": item_result.content_text,
        "author": item_result.author,
        "published_at": item_result.published_at,
    }


async def _create_feed_items(
    repo: RSSFeedRepositoryAdapter,
    *,
    feed_id: int,
    item_results: list[Any],
) -> list[tuple[dict[str, Any], Any]]:
    payloads = [_feed_item_payload(item_result) for item_result in item_results]
    item_result_by_guid: dict[str, Any] = {}
    for item_result in item_results:
        item_result_by_guid.setdefault(item_result.external_id, item_result)

    try:
        items = await repo.async_create_feed_items(feed_id=feed_id, items=payloads)
    except Exception:
        logger.warning(
            "rss_bulk_item_create_failed",
            extra={"feed_id": feed_id, "item_count": len(payloads)},
            exc_info=True,
        )
        items = []
        for item_result in item_results:
            try:
                item = await repo.async_create_feed_item(
                    feed_id=feed_id,
                    guid=item_result.external_id,
                    title=item_result.title,
                    url=item_result.canonical_url,
                    content=item_result.content_text,
                    author=item_result.author,
                    published_at=item_result.published_at,
                )
                if item is not None:
                    items.append(item)
            except Exception:
                logger.warning(
                    "rss_item_create_failed",
                    extra={"feed_id": feed_id, "guid": item_result.external_id},
                    exc_info=True,
                )

    return [
        (item, item_result_by_guid[str(item["guid"])])
        for item in items
        if str(item["guid"]) in item_result_by_guid
    ]


async def _mirror_signal_feed_items(
    signal_repo: SignalSourceRepositoryAdapter,
    *,
    source_id: int,
    feed_items: list[tuple[dict[str, Any], Any]],
) -> None:
    payloads = [
        {
            "external_id": item_result.external_id,
            "canonical_url": item_result.canonical_url,
            "title": item_result.title,
            "content_text": item_result.content_text,
            "author": item_result.author,
            "published_at": item_result.published_at,
            "engagement": item_result.engagement,
            "metadata": {**item_result.metadata, "legacy_rss_item_id": item["id"]},
        }
        for item, item_result in feed_items
    ]
    try:
        await signal_repo.async_upsert_feed_items(source_id=source_id, items=payloads)
    except Exception:
        logger.warning(
            "rss_bulk_signal_item_upsert_failed",
            extra={"source_id": source_id, "item_count": len(payloads)},
            exc_info=True,
        )
        for item, item_result in feed_items:
            try:
                await signal_repo.async_upsert_feed_item(
                    source_id=source_id,
                    external_id=item_result.external_id,
                    canonical_url=item_result.canonical_url,
                    title=item_result.title,
                    content_text=item_result.content_text,
                    author=item_result.author,
                    published_at=item_result.published_at,
                    engagement=item_result.engagement,
                    metadata={**item_result.metadata, "legacy_rss_item_id": item["id"]},
                )
            except Exception:
                logger.warning(
                    "rss_signal_item_upsert_failed",
                    extra={"source_id": source_id, "guid": item_result.external_id},
                    exc_info=True,
                )


async def _sync_signal_subscriptions(
    repo: RSSFeedRepositoryAdapter,
    signal_repo: SignalSourceRepositoryAdapter,
    *,
    source_id: int,
    item_ids: list[int],
) -> None:
    if not item_ids:
        return

    try:
        targets = await repo.async_list_delivery_targets(item_ids)
    except Exception:
        logger.warning(
            "rss_delivery_target_lookup_failed",
            extra={"source_id": source_id, "item_count": len(item_ids)},
            exc_info=True,
        )
        return

    subscriber_ids: list[int] = []
    for target in targets:
        for subscriber_id in target.get("subscriber_ids", []):
            subscriber_ids.append(int(subscriber_id))

    try:
        await signal_repo.async_subscribe_many(source_id=source_id, user_ids=subscriber_ids)
    except Exception:
        logger.warning(
            "rss_bulk_signal_subscribe_failed",
            extra={"source_id": source_id, "subscriber_count": len(subscriber_ids)},
            exc_info=True,
        )
        seen: set[int] = set()
        for subscriber_id in subscriber_ids:
            if subscriber_id in seen:
                continue
            seen.add(subscriber_id)
            try:
                await signal_repo.async_subscribe(user_id=subscriber_id, source_id=source_id)
            except Exception:
                logger.warning(
                    "rss_signal_subscribe_failed",
                    extra={"source_id": source_id, "user_id": subscriber_id},
                    exc_info=True,
                )


async def _poll_feed(
    repo: RSSFeedRepositoryAdapter,
    signal_repo: SignalSourceRepositoryAdapter,
    feed: dict[str, Any],
) -> _FeedPollResult:
    signal_source: dict[str, Any] | None = None
    try:
        feed_url = str(feed.get("url") or "")
        signal_source = await signal_repo.async_upsert_source(
            kind="substack" if is_substack_url(feed_url) else "rss",
            external_id=feed_url,
            url=feed.get("url"),
            title=feed.get("title"),
            description=feed.get("description"),
            site_url=feed.get("site_url"),
            metadata={
                "etag": feed.get("etag"),
                "last_modified": feed.get("last_modified"),
                "legacy_rss_feed_id": feed.get("id"),
            },
        )
        run_state = await signal_repo.async_get_source_run_state(int(signal_source["id"]))
        if not _legacy_rss_source_due(run_state):
            return _FeedPollResult(skipped=1)

        ingester = RssSignalIngester(feed, fetcher=fetch_feed)
        result = await ingester.fetch()
        signal_source = await signal_repo.async_upsert_source(
            kind=result.source.kind,
            external_id=result.source.external_id,
            url=result.source.url,
            title=result.source.title,
            description=result.source.description,
            site_url=result.source.site_url,
            metadata=result.source.metadata,
        )

        if result.not_modified:
            await signal_repo.async_record_source_fetch_success(int(signal_source["id"]))
            return _FeedPollResult(skipped=1)

        max_items_per_run = _max_items_per_run(run_state)
        created_items = await _create_feed_items(
            repo,
            feed_id=int(feed["id"]),
            item_results=list(result.items)[:max_items_per_run],
        )
        created_item_ids = [int(item["id"]) for item, _item_result in created_items]
        await _mirror_signal_feed_items(
            signal_repo,
            source_id=int(signal_source["id"]),
            feed_items=created_items,
        )
        await _sync_signal_subscriptions(
            repo,
            signal_repo,
            source_id=int(signal_source["id"]),
            item_ids=created_item_ids,
        )

        await repo.async_update_feed_fetch_success(
            feed_id=int(feed["id"]),
            title=result.source.title,
            description=result.source.description,
            site_url=result.source.site_url,
            etag=result.source.metadata.get("etag"),
            last_modified=result.source.metadata.get("last_modified"),
        )
        await signal_repo.async_record_source_fetch_success(int(signal_source["id"]))
        return _FeedPollResult(
            polled=1,
            new_items=len(created_items),
            new_item_ids=created_item_ids,
        )
    except Exception as exc:
        await repo.async_record_feed_fetch_error(
            feed_id=int(feed["id"]),
            error=str(exc),
            max_fetch_errors=MAX_FETCH_ERRORS,
        )
        if signal_source is not None:
            await signal_repo.async_record_source_fetch_error(
                source_id=int(signal_source["id"]),
                error=str(exc),
                max_errors=MAX_FETCH_ERRORS,
                base_backoff_seconds=SIGNAL_SOURCE_BASE_BACKOFF_SECONDS,
            )
        logger.warning(
            "rss_feed_poll_error",
            extra={
                "feed_id": feed.get("id"),
                "url": feed.get("url"),
                "error": str(exc)[:200],
            },
        )
        return _FeedPollResult(errors=1)


def _feed_host(feed: dict[str, Any]) -> str:
    url = str(feed.get("url") or "")
    return (urlparse(url).hostname or url).lower()


async def poll_all_feeds(
    db: Database,
    *,
    limit: int | None = None,
    concurrency: int = 8,
) -> dict:
    """Poll active RSS feeds for new items.

    ``limit`` caps how many feeds one cycle loads (least-recently-fetched first),
    bounding memory and work per poll; ``None`` loads every active feed.
    ``concurrency`` bounds total in-flight feed pipelines, while feeds sharing a
    hostname remain serialized so parallel polling does not amplify origin load.
    """
    repo = RSSFeedRepositoryAdapter(db)
    signal_repo = SignalSourceRepositoryAdapter(db)
    feeds = await repo.async_list_active_feeds(limit=limit)

    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    host_locks: dict[str, asyncio.Lock] = {}

    async def _bounded_poll(feed: dict[str, Any]) -> _FeedPollResult:
        host_lock = host_locks.setdefault(_feed_host(feed), asyncio.Lock())
        # Serializing first by host prevents duplicate feeds from consuming all
        # global permits while waiting for the same origin.
        async with host_lock, semaphore:
            return await _poll_feed(repo, signal_repo, feed)

    results = await asyncio.gather(*(_bounded_poll(feed) for feed in feeds))
    stats: dict[str, Any] = {
        "polled": sum(result.polled for result in results),
        "new_items": sum(result.new_items for result in results),
        "errors": sum(result.errors for result in results),
        "skipped": sum(result.skipped for result in results),
        "new_item_ids": [item_id for result in results for item_id in result.new_item_ids],
    }
    logger.info("rss_poll_complete", extra={k: v for k, v in stats.items() if k != "new_item_ids"})
    return stats


def _legacy_rss_source_due(run_state: dict[str, Any] | None) -> bool:
    if run_state is None:
        return True
    if not run_state.get("is_active"):
        return False
    backoff_until = run_state.get("backoff_until")
    return not isinstance(backoff_until, datetime) or backoff_until <= datetime.now(UTC)


def _max_items_per_run(run_state: dict[str, Any] | None) -> int:
    if run_state is None:
        return 100
    value = run_state.get("max_items_per_run")
    if value is None:
        return 100
    return max(1, min(int(value), 500))
