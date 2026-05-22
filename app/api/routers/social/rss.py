"""RSS feed management endpoints."""

from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter, Depends, Query, UploadFile
from starlette.responses import StreamingResponse

from app.api.exceptions import ResourceNotFoundError, ValidationError
from app.api.models.responses import success_response
from app.api.routers.auth import get_current_user
from app.api.search_helpers import isotime
from app.core.logging_utils import get_logger

logger = get_logger(__name__)

router = APIRouter()
MAX_FETCH_ERRORS = 10


def _get_rss_repo() -> Any:
    """Lazily obtain the RSS feed repository from the current API runtime."""
    from app.di.api import get_current_api_runtime

    runtime = get_current_api_runtime()
    return runtime.rss_feed_repo


async def _require_feed_subscription(repo: Any, *, user_id: int, feed_id: int) -> dict[str, Any]:
    """Return the user's subscription for a feed or raise a non-enumerating 404."""
    subscription = await repo.async_get_subscription_by_feed(user_id=user_id, feed_id=feed_id)
    if subscription is None:
        raise ResourceNotFoundError("Feed", feed_id)
    return dict(subscription)


# --- Subscription endpoints ---


@router.get("/feeds")
async def list_feeds(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List user's RSS feed subscriptions."""
    repo = _get_rss_repo()
    subs = await repo.async_list_user_subscriptions(user["user_id"])
    items = []
    for s in subs:
        items.append(
            {
                "subscription_id": s["id"],
                "feed_id": s.get("feed"),
                "feed_title": s.get("feed_title"),
                "feed_url": s.get("feed_url"),
                "site_url": s.get("site_url"),
                "category_name": s.get("category_name"),
                "is_active": s.get("is_active", True),
                "created_at": isotime(s.get("created_at")),
            }
        )
    return success_response({"feeds": items})


@router.post("/feeds/subscribe", status_code=201)
async def subscribe(
    body: dict[str, Any],
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Subscribe to an RSS feed by URL."""
    url = body.get("url")
    if not url or not isinstance(url, str):
        raise ValidationError("url is required")

    category_id = body.get("category_id")

    # Validate feed by fetching it
    from app.adapters.rss.feed_fetcher import fetch_feed

    try:
        result = fetch_feed(url)
    except Exception as exc:
        raise ValidationError(f"Could not fetch feed: {exc}") from exc

    repo = _get_rss_repo()

    # Get or create the feed record
    feed = await repo.async_get_or_create_feed(url)
    feed_id = feed["id"]

    # Update feed metadata from fetch result
    await repo.async_update_feed(
        feed_id,
        title=result.title or feed.get("title"),
        description=result.description or feed.get("description"),
        site_url=result.site_url or feed.get("site_url"),
        etag=result.etag,
        last_modified=result.last_modified,
    )

    # Store initial items
    for entry in result.entries:
        await repo.async_create_feed_item(
            feed_id=feed_id,
            guid=entry.guid,
            title=entry.title,
            url=entry.url,
            content=entry.content,
            author=entry.author,
            published_at=entry.published_at,
        )

    # Create subscription
    sub = await repo.async_create_subscription(
        user_id=user["user_id"],
        feed_id=feed_id,
        category_id=category_id,
    )
    return success_response(
        {
            "subscription_id": sub["id"],
            "feed_id": feed_id,
            "feed_title": result.title,
            "feed_url": url,
        }
    )


@router.delete("/feeds/{subscription_id}")
async def unsubscribe(
    subscription_id: int,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Unsubscribe from an RSS feed (verify ownership)."""
    repo = _get_rss_repo()

    # Verify ownership by checking user's subscriptions
    subs = await repo.async_list_user_subscriptions(user["user_id"])
    owned = any(s["id"] == subscription_id for s in subs)
    if not owned:
        raise ResourceNotFoundError("Subscription", subscription_id)

    await repo.async_delete_subscription(subscription_id)
    return success_response({"deleted": True, "id": subscription_id})


# --- Feed item endpoints ---


@router.get("/feeds/{feed_id}/items")
async def list_feed_items(
    feed_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List paginated items for a feed."""
    repo = _get_rss_repo()

    await _require_feed_subscription(repo, user_id=user["user_id"], feed_id=feed_id)

    items = await repo.async_list_feed_items(feed_id, limit=limit, offset=offset)
    return success_response(
        {
            "feed_id": feed_id,
            "items": [
                {
                    "id": item["id"],
                    "guid": item.get("guid"),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "author": item.get("author"),
                    "published_at": isotime(item.get("published_at")),
                    "created_at": isotime(item.get("created_at")),
                }
                for item in items
            ],
        }
    )


@router.post("/feeds/{feed_id}/refresh")
async def refresh_feed(
    feed_id: int,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Trigger a fetch for a specific feed and store new items."""
    repo = _get_rss_repo()

    await _require_feed_subscription(repo, user_id=user["user_id"], feed_id=feed_id)
    feed = await repo.async_get_feed(feed_id)
    if feed is None:
        raise ResourceNotFoundError("Feed", feed_id)

    from app.adapters.rss.feed_fetcher import fetch_feed

    try:
        result = fetch_feed(
            feed["url"],
            etag=feed.get("etag"),
            last_modified=feed.get("last_modified"),
        )
    except Exception as exc:
        await repo.async_record_feed_fetch_error(
            feed_id=feed_id,
            error=str(exc),
            max_fetch_errors=MAX_FETCH_ERRORS,
        )
        raise ValidationError(f"Feed fetch failed: {exc}") from exc

    if result.not_modified:
        return success_response({"feed_id": feed_id, "new_items": 0, "not_modified": True})

    new_count = 0
    for entry in result.entries:
        item = await repo.async_create_feed_item(
            feed_id=feed_id,
            guid=entry.guid,
            title=entry.title,
            url=entry.url,
            content=entry.content,
            author=entry.author,
            published_at=entry.published_at,
        )
        if item is not None:
            new_count += 1

    # Update feed metadata
    from datetime import datetime

    from app.core.time_utils import UTC

    now = datetime.now(UTC)
    await repo.async_update_feed(
        feed_id,
        title=result.title or feed.get("title"),
        description=result.description or feed.get("description"),
        site_url=result.site_url or feed.get("site_url"),
        last_fetched_at=now,
        last_successful_at=now,
        etag=result.etag,
        last_modified=result.last_modified,
        fetch_error_count=0,
        last_error=None,
    )

    return success_response({"feed_id": feed_id, "new_items": new_count})


# --- OPML import/export ---


@router.get("/export/opml")
async def export_opml(
    user: dict[str, Any] = Depends(get_current_user),
) -> StreamingResponse:
    """Export user's RSS subscriptions as OPML 2.0."""
    repo = _get_rss_repo()
    subs = await repo.async_list_user_subscriptions(user["user_id"])

    # Build feed dicts for OPMLExporter
    feeds = []
    for s in subs:
        feeds.append(
            {
                "url": s.get("feed_url", ""),
                "title": s.get("feed_title"),
                "site_url": s.get("site_url"),
                "category_name": s.get("category_name"),
            }
        )

    from app.domain.services.import_export import OPMLExporter

    exporter = OPMLExporter()
    xml_content = exporter.serialize(feeds)

    return StreamingResponse(
        io.BytesIO(xml_content.encode("utf-8")),
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=subscriptions.opml"},
    )


@router.post("/import/opml")
async def import_opml(
    file: UploadFile,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Import OPML file and subscribe to each feed URL."""
    content = await file.read()
    if not content:
        raise ValidationError("Empty file")

    from app.domain.services.import_parsers.opml import OPMLParser

    parser = OPMLParser()
    bookmarks = parser.parse(content)

    if not bookmarks:
        raise ValidationError("No feeds found in OPML file")

    repo = _get_rss_repo()
    imported = 0
    errors = 0

    for bm in bookmarks:
        try:
            feed = await repo.async_get_or_create_feed(bm.url)
            await repo.async_create_subscription(
                user_id=user["user_id"],
                feed_id=feed["id"],
            )
            imported += 1
        except Exception:
            errors += 1
            logger.warning(
                "opml_import_feed_error",
                extra={"url": bm.url, "user_id": user["user_id"]},
            )

    return success_response(
        {
            "imported": imported,
            "errors": errors,
            "total": len(bookmarks),
        }
    )
