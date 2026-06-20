"""SQLAlchemy implementation of the RSS feed repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import (
    ChannelCategory,
    RSSFeed,
    RSSFeedItem,
    RSSFeedSubscription,
    RSSItemDelivery,
    model_to_dict,
)
from app.db.types import _utcnow

if TYPE_CHECKING:
    from datetime import datetime

    from app.db.session import Database


class RSSFeedRepositoryAdapter:
    """Adapter for RSS feed, subscription, and feed item operations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    # --- Feed CRUD ---

    async def async_get_or_create_feed(self, url: str) -> dict[str, Any]:
        """Find a feed by URL or create a new one."""
        async with self._database.transaction() as session:
            stmt = (
                insert(RSSFeed)
                .values(url=url)
                .on_conflict_do_nothing(index_elements=[RSSFeed.url])
                .returning(RSSFeed)
            )
            feed = await session.scalar(stmt)
            if feed is None:
                feed = await session.scalar(select(RSSFeed).where(RSSFeed.url == url))
            return model_to_dict(feed) or {}

    async def async_get_feed(self, feed_id: int) -> dict[str, Any] | None:
        """Return a feed by ID."""
        async with self._database.session() as session:
            feed = await session.get(RSSFeed, feed_id)
            return model_to_dict(feed)

    async def async_update_feed(self, feed_id: int, **fields: Any) -> None:
        """Update feed fields by ID."""
        allowed_fields = set(RSSFeed.__mapper__.columns.keys()) - {"id", "created_at"}
        update_data = {key: value for key, value in fields.items() if key in allowed_fields}
        if not update_data:
            return
        update_data["updated_at"] = _utcnow()
        async with self._database.transaction() as session:
            await session.execute(
                update(RSSFeed).where(RSSFeed.id == feed_id).values(**update_data)
            )

    async def async_list_active_feeds(self) -> list[dict[str, Any]]:
        """Return feeds that have at least one active subscription."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(RSSFeed)
                    .join(RSSFeedSubscription, RSSFeedSubscription.feed_id == RSSFeed.id)
                    .where(RSSFeed.is_active.is_(True), RSSFeedSubscription.is_active.is_(True))
                    .distinct()
                )
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    # --- Subscription CRUD ---

    async def async_create_subscription(
        self,
        user_id: int,
        feed_id: int,
        category_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a subscription for a user to a feed."""
        async with self._database.transaction() as session:
            stmt = (
                insert(RSSFeedSubscription)
                .values(user_id=user_id, feed_id=feed_id, category_id=category_id)
                .on_conflict_do_nothing(
                    index_elements=[
                        RSSFeedSubscription.user_id,
                        RSSFeedSubscription.feed_id,
                    ]
                )
                .returning(RSSFeedSubscription)
            )
            subscription = await session.scalar(stmt)
            if subscription is None:
                subscription = await session.scalar(
                    select(RSSFeedSubscription).where(
                        RSSFeedSubscription.user_id == user_id,
                        RSSFeedSubscription.feed_id == feed_id,
                    )
                )
            return self._subscription_dict(subscription)

    async def async_delete_subscription(self, subscription_id: int) -> None:
        """Delete a subscription by ID."""
        async with self._database.transaction() as session:
            subscription = await session.get(RSSFeedSubscription, subscription_id)
            if subscription is not None:
                await session.delete(subscription)

    async def async_delete_subscription_for_user(
        self, *, user_id: int, subscription_id: int
    ) -> bool:
        """Delete a subscription only if it belongs to user_id.

        Returns True when a row was deleted, False when no matching row existed
        (either the subscription does not exist or it belongs to a different user).
        """
        async with self._database.transaction() as session:
            result = await session.execute(
                delete(RSSFeedSubscription)
                .where(
                    RSSFeedSubscription.id == subscription_id,
                    RSSFeedSubscription.user_id == user_id,
                )
                .returning(RSSFeedSubscription.id)
            )
            return result.first() is not None

    async def async_list_user_subscriptions(self, user_id: int) -> list[dict[str, Any]]:
        """Return all subscriptions for a user, joined with feed details."""
        async with self._database.session() as session:
            rows = await session.execute(
                select(RSSFeedSubscription, RSSFeed, ChannelCategory)
                .join(RSSFeed, RSSFeedSubscription.feed_id == RSSFeed.id)
                .outerjoin(ChannelCategory, RSSFeedSubscription.category_id == ChannelCategory.id)
                .where(RSSFeedSubscription.user_id == user_id)
                .order_by(RSSFeedSubscription.created_at.desc())
            )
            return [
                self._subscription_dict(
                    subscription,
                    feed=feed,
                    category=category,
                    include_flat_feed=True,
                )
                for subscription, feed, category in rows
            ]

    async def async_list_user_active_subscriptions(
        self,
        user_id: int,
        *,
        substack_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return active subscriptions for a user, optionally filtered to Substack."""
        async with self._database.session() as session:
            stmt = (
                select(RSSFeedSubscription, RSSFeed, ChannelCategory)
                .join(RSSFeed, RSSFeedSubscription.feed_id == RSSFeed.id)
                .outerjoin(ChannelCategory, RSSFeedSubscription.category_id == ChannelCategory.id)
                .where(
                    RSSFeedSubscription.user_id == user_id,
                    RSSFeedSubscription.is_active.is_(True),
                )
                .order_by(RSSFeedSubscription.created_at.desc())
            )
            if substack_only:
                stmt = stmt.where(RSSFeed.url.contains("substack.com"))
            rows = await session.execute(stmt)
            return [
                self._subscription_dict(
                    subscription,
                    feed=feed,
                    category=category,
                    include_feed=True,
                )
                for subscription, feed, category in rows
            ]

    async def async_get_subscription_by_feed(
        self, *, user_id: int, feed_id: int
    ) -> dict[str, Any] | None:
        """Return a user's subscription for a feed."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(RSSFeedSubscription, RSSFeed)
                    .join(RSSFeed, RSSFeedSubscription.feed_id == RSSFeed.id)
                    .where(
                        RSSFeedSubscription.user_id == user_id,
                        RSSFeedSubscription.feed_id == feed_id,
                    )
                )
            ).first()
            if row is None:
                return None
            subscription, feed = row
            return self._subscription_dict(subscription, feed=feed, include_feed=True)

    async def async_get_subscription(
        self, *, user_id: int, subscription_id: int
    ) -> dict[str, Any] | None:
        """Return a user's subscription by subscription ID."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(RSSFeedSubscription, RSSFeed, ChannelCategory)
                    .join(RSSFeed, RSSFeedSubscription.feed_id == RSSFeed.id)
                    .outerjoin(
                        ChannelCategory, RSSFeedSubscription.category_id == ChannelCategory.id
                    )
                    .where(
                        RSSFeedSubscription.id == subscription_id,
                        RSSFeedSubscription.user_id == user_id,
                    )
                )
            ).first()
            if row is None:
                return None
            subscription, feed, category = row
            return self._subscription_dict(
                subscription,
                feed=feed,
                category=category,
                include_feed=True,
            )

    async def async_set_subscription_active(
        self,
        subscription_id: int,
        *,
        is_active: bool,
        user_id: int | None = None,
    ) -> None:
        """Update subscription active state.

        When user_id is provided it is included in the WHERE clause so the
        update only applies when the subscription belongs to that user
        (defense-in-depth IDOR guard).
        """
        async with self._database.transaction() as session:
            predicates = [RSSFeedSubscription.id == subscription_id]
            if user_id is not None:
                predicates.append(RSSFeedSubscription.user_id == user_id)
            await session.execute(
                update(RSSFeedSubscription)
                .where(*predicates)
                .values(is_active=is_active, updated_at=_utcnow())
            )

    # --- Feed items ---

    async def async_create_feed_item(
        self,
        feed_id: int,
        guid: str,
        title: str | None,
        url: str | None,
        content: str | None,
        author: str | None,
        published_at: datetime | None,
    ) -> dict[str, Any] | None:
        """Insert a feed item, ignoring duplicates by feed and GUID."""
        async with self._database.transaction() as session:
            stmt = (
                insert(RSSFeedItem)
                .values(
                    feed_id=feed_id,
                    guid=guid,
                    title=title,
                    url=url,
                    content=content,
                    author=author,
                    published_at=published_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        RSSFeedItem.feed_id,
                        RSSFeedItem.guid,
                    ]
                )
                .returning(RSSFeedItem)
            )
            item = await session.scalar(stmt)
            return self._feed_item_dict(item) if item is not None else None

    async def async_create_feed_items(
        self,
        feed_id: int,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert feed items in bulk, ignoring duplicates by feed and GUID."""
        if not items:
            return []

        values: list[dict[str, Any]] = []
        seen_guids: set[str] = set()
        for item in items:
            guid = str(item["guid"])
            if guid in seen_guids:
                continue
            seen_guids.add(guid)
            values.append(
                {
                    "feed_id": feed_id,
                    "guid": guid,
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "content": item.get("content"),
                    "author": item.get("author"),
                    "published_at": item.get("published_at"),
                }
            )

        if not values:
            return []

        async with self._database.transaction() as session:
            stmt = (
                insert(RSSFeedItem)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        RSSFeedItem.feed_id,
                        RSSFeedItem.guid,
                    ]
                )
                .returning(RSSFeedItem)
            )
            inserted = list((await session.execute(stmt)).scalars())

        inserted_by_guid = {item.guid: self._feed_item_dict(item) for item in inserted}
        return [
            inserted_by_guid[record["guid"]]
            for record in values
            if record["guid"] in inserted_by_guid
        ]

    async def async_list_feed_items(
        self,
        feed_id: int,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return paginated feed items for a feed."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(RSSFeedItem)
                    .where(RSSFeedItem.feed_id == feed_id)
                    .order_by(RSSFeedItem.published_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars()
            return [self._feed_item_dict(row) for row in rows]

    async def async_list_delivery_targets(
        self,
        new_item_ids: list[int] | None,
    ) -> list[dict[str, Any]]:
        """Return undelivered item rows with subscriber IDs."""
        async with self._database.session() as session:
            stmt = (
                select(RSSFeedItem, RSSFeedSubscription.user_id)
                .join(
                    RSSFeedSubscription,
                    RSSFeedSubscription.feed_id == RSSFeedItem.feed_id,
                )
                .outerjoin(
                    RSSItemDelivery,
                    (RSSItemDelivery.item_id == RSSFeedItem.id)
                    & (RSSItemDelivery.user_id == RSSFeedSubscription.user_id),
                )
                .where(
                    RSSFeedSubscription.is_active.is_(True),
                    RSSItemDelivery.id.is_(None),
                )
                .order_by(
                    RSSFeedItem.published_at.desc(),
                    RSSFeedSubscription.created_at.asc(),
                )
            )
            if new_item_ids:
                stmt = stmt.where(RSSFeedItem.id.in_(new_item_ids))

            rows = await session.execute(stmt)
            grouped: dict[int, dict[str, Any]] = {}
            for item, user_id in rows:
                item_id = int(item.id)
                item_dict = grouped.get(item_id)
                if item_dict is None:
                    item_dict = self._feed_item_dict(item)
                    item_dict["subscriber_ids"] = []
                    grouped[item_id] = item_dict
                item_dict["subscriber_ids"].append(user_id)
            return list(grouped.values())

    async def async_mark_item_delivered(self, *, user_id: int, item_id: int) -> None:
        """Create an RSS delivery record for a user and item."""
        async with self._database.transaction() as session:
            await session.execute(
                insert(RSSItemDelivery)
                .values(user_id=user_id, item_id=item_id)
                .on_conflict_do_nothing(
                    index_elements=[
                        RSSItemDelivery.user_id,
                        RSSItemDelivery.item_id,
                    ]
                )
            )

    async def async_mark_items_delivered(self, deliveries: list[tuple[int, int]]) -> None:
        """Create RSS delivery records for user/item pairs in bulk."""
        if not deliveries:
            return

        values: list[dict[str, int]] = []
        seen: set[tuple[int, int]] = set()
        for user_id, item_id in deliveries:
            key = (int(user_id), int(item_id))
            if key in seen:
                continue
            seen.add(key)
            values.append({"user_id": key[0], "item_id": key[1]})

        if not values:
            return

        async with self._database.transaction() as session:
            await session.execute(
                insert(RSSItemDelivery)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        RSSItemDelivery.user_id,
                        RSSItemDelivery.item_id,
                    ]
                )
            )

    async def async_update_feed_fetch_success(
        self,
        *,
        feed_id: int,
        title: str | None,
        description: str | None,
        site_url: str | None,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        """Update feed metadata after a successful poll."""
        now = _utcnow()
        async with self._database.transaction() as session:
            await session.execute(
                update(RSSFeed)
                .where(RSSFeed.id == feed_id)
                .values(
                    title=title,
                    description=description,
                    site_url=site_url,
                    last_fetched_at=now,
                    last_successful_at=now,
                    etag=etag,
                    last_modified=last_modified,
                    fetch_error_count=0,
                    last_error=None,
                    updated_at=now,
                )
            )

    async def async_record_feed_fetch_error(
        self,
        *,
        feed_id: int,
        error: str,
        max_fetch_errors: int,
    ) -> None:
        """Increment RSS feed error counters and disable on threshold."""
        async with self._database.transaction() as session:
            error_count = int(
                await session.scalar(select(RSSFeed.fetch_error_count).where(RSSFeed.id == feed_id))
                or 0
            )
            error_count += 1
            update_values: dict[str, Any] = {
                "fetch_error_count": error_count,
                "last_error": error[:500],
                "updated_at": _utcnow(),
            }
            if error_count >= max_fetch_errors:
                update_values["is_active"] = False
            await session.execute(
                update(RSSFeed).where(RSSFeed.id == feed_id).values(**update_values)
            )

    def _subscription_dict(
        self,
        subscription: RSSFeedSubscription | None,
        *,
        feed: RSSFeed | None = None,
        category: ChannelCategory | None = None,
        include_feed: bool = False,
        include_flat_feed: bool = False,
    ) -> dict[str, Any]:
        data = model_to_dict(subscription) or {}
        if not data:
            return data
        data["user"] = data.get("user_id")
        data["feed"] = (
            model_to_dict(feed) if include_feed and feed is not None else data.get("feed_id")
        )
        data["category"] = data.get("category_id")
        data["category_name"] = category.name if category is not None else None
        if include_flat_feed and feed is not None:
            data["feed_title"] = feed.title
            data["feed_url"] = feed.url
            data["site_url"] = feed.site_url
            data["feed_description"] = feed.description
        return data

    def _feed_item_dict(self, item: RSSFeedItem) -> dict[str, Any]:
        data = model_to_dict(item) or {}
        data["feed"] = data.get("feed_id")
        return data
