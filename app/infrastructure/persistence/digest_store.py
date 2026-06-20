"""Shared digest access for channels, posts, categories, and deliveries."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar, cast

from sqlalchemy import Integer, cast as sa_cast, distinct, func, insert, select, update
from sqlalchemy.orm import selectinload

from app.core.time_utils import utc_now
from app.db.models import (
    Channel,
    ChannelCategory,
    ChannelPost,
    ChannelPostAnalysis,
    ChannelSubscription,
    DigestDelivery,
    FeedItem,
    Source,
    Subscription,
    UserDigestPreference,
    _utcnow,
)
from app.db.runtime_database import resolve_runtime_database

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from app.db.session import Database

T = TypeVar("T")


def _run_sync(coro: Coroutine[Any, Any, T]) -> T:
    # All sync wrapper methods on DigestStore are designed to be called from a
    # genuinely synchronous context -- either via asyncio.to_thread() from the
    # FastAPI router layer (routers/social/digest.py wraps every sync call with
    # to_thread so the thread has no running event loop), or from the Telethon
    # userbot which dispatches sync helpers from its own separate thread.
    #
    # Calling asyncio.run() from inside a running event loop raises:
    #   "asyncio.run() cannot be called from a running event loop"
    # Detect this early and raise a clear error rather than surfacing an
    # opaque RuntimeError from deep inside asyncio, matching the pattern used
    # by app.db.runtime.maintenance._run_sync and inspection._run_sync.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    msg = (
        "DigestStore synchronous wrappers cannot be called from inside a running event loop. "
        "Use the async_ counterpart (e.g. async_list_active_subscriptions) instead."
    )
    raise RuntimeError(msg)


class DigestStore:
    """Centralized ORM access for digest runtime features."""

    def __init__(self, database: Database | None = None) -> None:
        self._db = database

    def _database(self) -> Database:
        if self._db is not None:
            return self._db
        return resolve_runtime_database()

    async def async_list_active_subscriptions(self, user_id: int) -> list[ChannelSubscription]:
        async with self._database().session() as session:
            return list(
                (
                    await session.execute(
                        select(ChannelSubscription)
                        .options(
                            selectinload(ChannelSubscription.channel),
                            selectinload(ChannelSubscription.category),
                        )
                        .where(
                            ChannelSubscription.user_id == user_id,
                            ChannelSubscription.is_active.is_(True),
                        )
                        .order_by(ChannelSubscription.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )

    def list_active_subscriptions(self, user_id: int) -> list[Any]:
        return _run_sync(self.async_list_active_subscriptions(user_id))

    async def async_list_fetchable_subscriptions(self, user_id: int) -> list[ChannelSubscription]:
        subscriptions = await self.async_list_active_subscriptions(user_id)
        channels = [sub.channel for sub in subscriptions if sub.channel is not None]
        if not channels:
            return []

        # Resolve run-state for every channel in a fixed number of queries
        # (one transaction), instead of opening a transaction + several SELECTs
        # per subscription.
        async with self._database().transaction() as session:
            run_states = await self._batch_channel_run_states(
                session, user_id=user_id, channels=channels
            )

        return [
            sub
            for sub in subscriptions
            if sub.channel is not None and _channel_source_due(run_states.get(sub.channel.id, {}))
        ]

    async def async_count_active_subscriptions(self, user_id: int) -> int:
        async with self._database().session() as session:
            return int(
                await session.scalar(
                    select(func.count(ChannelSubscription.id)).where(
                        ChannelSubscription.user_id == user_id,
                        ChannelSubscription.is_active.is_(True),
                    )
                )
                or 0
            )

    def count_active_subscriptions(self, user_id: int) -> int:
        return _run_sync(self.async_count_active_subscriptions(user_id))

    async def async_count_active_subscriptions_for_category(self, category: Any) -> int:
        async with self._database().session() as session:
            return int(
                await session.scalar(
                    select(func.count(ChannelSubscription.id)).where(
                        ChannelSubscription.category_id == category.id,
                        ChannelSubscription.is_active.is_(True),
                    )
                )
                or 0
            )

    def count_active_subscriptions_for_category(self, category: Any) -> int:
        return _run_sync(self.async_count_active_subscriptions_for_category(category))

    async def async_get_category_for_user(
        self, user_id: int, category_id: int
    ) -> ChannelCategory | None:
        async with self._database().session() as session:
            return await session.scalar(
                select(ChannelCategory).where(
                    ChannelCategory.id == category_id,
                    ChannelCategory.user_id == user_id,
                )
            )

    def get_category_for_user(self, user_id: int, category_id: int) -> Any | None:
        return _run_sync(self.async_get_category_for_user(user_id, category_id))

    async def async_list_categories(self, user_id: int) -> list[ChannelCategory]:
        async with self._database().session() as session:
            return list(
                (
                    await session.execute(
                        select(ChannelCategory)
                        .where(ChannelCategory.user_id == user_id)
                        .order_by(ChannelCategory.position, ChannelCategory.name)
                    )
                )
                .scalars()
                .all()
            )

    def list_categories(self, user_id: int) -> list[Any]:
        return _run_sync(self.async_list_categories(user_id))

    async def async_next_category_position(self, user_id: int) -> int:
        async with self._database().session() as session:
            max_pos = await session.scalar(
                select(func.max(ChannelCategory.position)).where(ChannelCategory.user_id == user_id)
            )
            return (max_pos or 0) + 1

    def next_category_position(self, user_id: int) -> int:
        return _run_sync(self.async_next_category_position(user_id))

    async def async_create_category(
        self, *, user_id: int, name: str, position: int
    ) -> ChannelCategory:
        async with self._database().transaction() as session:
            category = ChannelCategory(user_id=user_id, name=name, position=position)
            session.add(category)
            await session.flush()
            return category

    def create_category(self, *, user_id: int, name: str, position: int) -> Any:
        return _run_sync(self.async_create_category(user_id=user_id, name=name, position=position))

    async def async_save_model(self, instance: Any) -> None:
        """Persist an ORM instance via session.merge().

        This is a generic last-writer-wins upsert: whichever caller commits last
        wins. There is no version column or optimistic-lock guard; concurrent
        saves to the same row can silently overwrite each other's changes.

        Deferred: adding a `version` column for optimistic locking is tracked
        separately and requires a schema migration. Until then callers should
        ensure they do not hold stale in-memory instances across long-lived
        operations before calling save_model.
        """
        if hasattr(instance, "updated_at"):
            instance.updated_at = _utcnow()
        async with self._database().transaction() as session:
            await session.merge(instance)

    def save_model(self, instance: Any) -> None:
        _run_sync(self.async_save_model(instance))

    async def async_delete_model(self, instance: Any) -> None:
        async with self._database().transaction() as session:
            persistent = await session.merge(instance)
            await session.delete(persistent)

    def delete_model(self, instance: Any) -> None:
        _run_sync(self.async_delete_model(instance))

    async def async_get_subscription_for_user(
        self, *, user_id: int, subscription_id: int
    ) -> ChannelSubscription | None:
        async with self._database().session() as session:
            return await session.scalar(
                select(ChannelSubscription)
                .options(
                    selectinload(ChannelSubscription.channel),
                    selectinload(ChannelSubscription.category),
                )
                .where(
                    ChannelSubscription.id == subscription_id,
                    ChannelSubscription.user_id == user_id,
                )
            )

    def get_subscription_for_user(self, *, user_id: int, subscription_id: int) -> Any | None:
        return _run_sync(
            self.async_get_subscription_for_user(
                user_id=user_id,
                subscription_id=subscription_id,
            )
        )

    async def async_list_category_subscriptions(
        self,
        *,
        user_id: int,
        subscription_ids: list[int],
    ) -> list[ChannelSubscription]:
        async with self._database().session() as session:
            return list(
                (
                    await session.execute(
                        select(ChannelSubscription)
                        .options(selectinload(ChannelSubscription.channel))
                        .where(
                            ChannelSubscription.id.in_(subscription_ids),
                            ChannelSubscription.user_id == user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )

    def list_category_subscriptions(
        self,
        *,
        user_id: int,
        subscription_ids: list[int],
    ) -> list[Any]:
        return _run_sync(
            self.async_list_category_subscriptions(
                user_id=user_id,
                subscription_ids=subscription_ids,
            )
        )

    async def async_get_or_create_channel(
        self, username: str, *, title: str | None = None
    ) -> Channel:
        async with self._database().transaction() as session:
            channel = await session.scalar(select(Channel).where(Channel.username == username))
            if channel is not None:
                return channel

            channel = Channel(username=username, title=title or username, is_active=True)
            session.add(channel)
            await session.flush()
            return channel

    def get_or_create_channel(self, username: str, *, title: str | None = None) -> Any:
        return _run_sync(self.async_get_or_create_channel(username, title=title))

    async def async_update_channel_metadata(self, channel: Any, metadata: dict[str, Any]) -> None:
        changed: dict[str, Any] = {}
        for field in ("title", "description", "member_count"):
            value = metadata.get(field)
            if value is not None and getattr(channel, field) != value:
                setattr(channel, field, value)
                changed[field] = value
        if changed:
            changed["updated_at"] = utc_now()
            async with self._database().transaction() as session:
                await session.execute(
                    update(Channel).where(Channel.id == channel.id).values(**changed)
                )

    def update_channel_metadata(self, channel: Any, metadata: dict[str, Any]) -> None:
        _run_sync(self.async_update_channel_metadata(channel, metadata))

    async def async_is_user_subscribed(self, *, user_id: int, channel: Any) -> bool:
        async with self._database().session() as session:
            return (
                await session.scalar(
                    select(ChannelSubscription.id).where(
                        ChannelSubscription.user_id == user_id,
                        ChannelSubscription.channel_id == channel.id,
                        ChannelSubscription.is_active.is_(True),
                    )
                )
                is not None
            )

    def is_user_subscribed(self, *, user_id: int, channel: Any) -> bool:
        return _run_sync(self.async_is_user_subscribed(user_id=user_id, channel=channel))

    async def async_get_channel_by_username(self, username: str) -> Channel | None:
        async with self._database().session() as session:
            return await session.scalar(select(Channel).where(Channel.username == username))

    def get_channel_by_username(self, username: str) -> Any | None:
        return _run_sync(self.async_get_channel_by_username(username))

    async def async_count_channel_posts(self, channel: Any) -> int:
        async with self._database().session() as session:
            return int(
                await session.scalar(
                    select(func.count(ChannelPost.id)).where(ChannelPost.channel_id == channel.id)
                )
                or 0
            )

    def count_channel_posts(self, channel: Any) -> int:
        return _run_sync(self.async_count_channel_posts(channel))

    async def async_list_channel_posts(
        self, channel: Any, *, limit: int, offset: int
    ) -> list[ChannelPost]:
        async with self._database().session() as session:
            return list(
                (
                    await session.execute(
                        select(ChannelPost)
                        .where(ChannelPost.channel_id == channel.id)
                        .order_by(ChannelPost.date.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    def list_channel_posts(self, channel: Any, *, limit: int, offset: int) -> list[Any]:
        return _run_sync(self.async_list_channel_posts(channel, limit=limit, offset=offset))

    async def async_get_post_analysis(self, post: Any) -> ChannelPostAnalysis | None:
        async with self._database().session() as session:
            return await session.scalar(
                select(ChannelPostAnalysis).where(ChannelPostAnalysis.post_id == post.id)
            )

    def get_post_analysis(self, post: Any) -> Any | None:
        return _run_sync(self.async_get_post_analysis(post))

    async def async_list_deliveries(
        self, *, user_id: int, limit: int, offset: int
    ) -> list[DigestDelivery]:
        async with self._database().session() as session:
            return list(
                (
                    await session.execute(
                        select(DigestDelivery)
                        .where(DigestDelivery.user_id == user_id)
                        .order_by(DigestDelivery.delivered_at.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    def list_deliveries(self, *, user_id: int, limit: int, offset: int) -> list[Any]:
        return _run_sync(self.async_list_deliveries(user_id=user_id, limit=limit, offset=offset))

    async def async_count_deliveries(self, user_id: int) -> int:
        async with self._database().session() as session:
            return int(
                await session.scalar(
                    select(func.count(DigestDelivery.id)).where(DigestDelivery.user_id == user_id)
                )
                or 0
            )

    def count_deliveries(self, user_id: int) -> int:
        return _run_sync(self.async_count_deliveries(user_id))

    async def async_get_user_preference(self, user_id: int) -> UserDigestPreference | None:
        async with self._database().session() as session:
            return await session.scalar(
                select(UserDigestPreference).where(UserDigestPreference.user_id == user_id)
            )

    def get_user_preference(self, user_id: int) -> Any | None:
        return _run_sync(self.async_get_user_preference(user_id))

    async def async_get_or_create_user_preference(
        self, user_id: int, defaults: dict[str, Any]
    ) -> tuple[UserDigestPreference, bool]:
        async with self._database().transaction() as session:
            preference = await session.scalar(
                select(UserDigestPreference).where(UserDigestPreference.user_id == user_id)
            )
            if preference is not None:
                return preference, False

            preference = UserDigestPreference(user_id=user_id, **defaults)
            session.add(preference)
            await session.flush()
            return preference, True

    def get_or_create_user_preference(
        self, user_id: int, defaults: dict[str, Any]
    ) -> tuple[Any, bool]:
        return _run_sync(self.async_get_or_create_user_preference(user_id, defaults))

    async def async_touch_preference(self, preference: Any) -> None:
        preference.updated_at = _utcnow()
        async with self._database().transaction() as session:
            await session.merge(preference)

    def touch_preference(self, preference: Any) -> None:
        _run_sync(self.async_touch_preference(preference))

    async def async_list_active_feed_subscriptions_with_channels(
        self, user_id: int
    ) -> list[ChannelSubscription]:
        async with self._database().session() as session:
            return list(
                (
                    await session.execute(
                        select(ChannelSubscription)
                        .join(Channel)
                        .options(selectinload(ChannelSubscription.channel))
                        .where(
                            ChannelSubscription.user_id == user_id,
                            ChannelSubscription.is_active.is_(True),
                            Channel.is_active.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )

    def list_active_feed_subscriptions_with_channels(self, user_id: int) -> list[Any]:
        return _run_sync(self.async_list_active_feed_subscriptions_with_channels(user_id))

    async def async_list_delivered_message_ids(self, user_id: int) -> set[int]:
        cutoff = utc_now() - timedelta(days=30)
        async with self._database().session() as session:
            # Unnest + DISTINCT server-side so only the distinct integer ids cross
            # the wire, instead of fetching every delivery's full posts_json blob
            # and flattening it in Python. jsonb_typeof guards non-array payloads
            # (matching the previous isinstance(list) check).
            post_id = sa_cast(func.jsonb_array_elements_text(DigestDelivery.posts_json), Integer)
            rows = (
                await session.execute(
                    select(distinct(post_id)).where(
                        DigestDelivery.user_id == user_id,
                        DigestDelivery.delivered_at >= cutoff,
                        func.jsonb_typeof(DigestDelivery.posts_json) == "array",
                    )
                )
            ).scalars()
            return {int(value) for value in rows if value is not None}

    def list_delivered_message_ids(self, user_id: int) -> set[int]:
        return _run_sync(self.async_list_delivered_message_ids(user_id))

    async def async_persist_posts(self, channel: Any, posts: list[dict[str, Any]]) -> None:
        message_ids = [post["message_id"] for post in posts]
        async with self._database().transaction() as session:
            existing_message_ids: set[int] = set()
            if message_ids:
                existing_message_ids = set(
                    (
                        await session.execute(
                            select(ChannelPost.message_id).where(
                                ChannelPost.channel_id == channel.id,
                                ChannelPost.message_id.in_(message_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

            queued_message_ids = set(existing_message_ids)
            new_rows: list[dict[str, Any]] = []
            for post in posts:
                message_id = post["message_id"]
                if message_id in queued_message_ids:
                    continue
                queued_message_ids.add(message_id)
                new_rows.append(
                    {
                        "channel_id": channel.id,
                        "message_id": message_id,
                        "text": post["text"],
                        "media_type": post.get("media_type"),
                        "date": post["date"],
                        "views": post.get("views"),
                        "forwards": post.get("forwards"),
                        "url": post.get("url"),
                        # created_at has a Python-side default; set it explicitly
                        # since a Core bulk insert does not run ORM-flush defaults.
                        "created_at": _utcnow(),
                    }
                )
            if new_rows:
                # One bulk INSERT (executemany) instead of per-row session.add.
                await session.execute(insert(ChannelPost), new_rows)

    def persist_posts(self, channel: Any, posts: list[dict[str, Any]]) -> None:
        _run_sync(self.async_persist_posts(channel, posts))

    async def async_mirror_posts_to_signal_sources(
        self,
        *,
        user_id: int,
        channel: Any,
        posts: list[dict[str, Any]],
    ) -> None:
        async with self._database().transaction() as session:
            source = await session.scalar(
                select(Source).where(
                    Source.kind == "telegram_channel",
                    Source.external_id == channel.username,
                )
            )
            if source is None:
                source = Source(kind="telegram_channel", external_id=channel.username)
                session.add(source)
                await session.flush()

            source.url = f"https://t.me/{channel.username}"
            source.title = channel.title
            source.description = channel.description
            source.is_active = channel.is_active
            source.fetch_error_count = channel.fetch_error_count
            source.last_error = channel.last_error
            source.last_fetched_at = channel.last_fetched_at
            source.metadata_json = {
                "channel_id": channel.channel_id,
                "member_count": channel.member_count,
            }
            source.legacy_channel_id = channel.id
            source.updated_at = _utcnow()

            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == user_id,
                    Subscription.source_id == source.id,
                )
            )
            if subscription is None:
                session.add(Subscription(user_id=user_id, source_id=source.id, is_active=True))

            message_ids = [post["message_id"] for post in posts]
            external_ids = [str(message_id) for message_id in message_ids]
            channel_posts_by_message_id: dict[int, ChannelPost] = {}
            feed_items_by_external_id: dict[str, FeedItem] = {}
            if message_ids:
                channel_posts_by_message_id = {
                    channel_post.message_id: channel_post
                    for channel_post in (
                        (
                            await session.execute(
                                select(ChannelPost).where(
                                    ChannelPost.channel_id == channel.id,
                                    ChannelPost.message_id.in_(message_ids),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                }
                feed_items_by_external_id = {
                    item.external_id: item
                    for item in (
                        (
                            await session.execute(
                                select(FeedItem).where(
                                    FeedItem.source_id == source.id,
                                    FeedItem.external_id.in_(external_ids),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                }

            for post in posts:
                message_id = post["message_id"]
                external_id = str(message_id)
                channel_post = channel_posts_by_message_id.get(message_id)
                item = feed_items_by_external_id.get(external_id)
                if item is None:
                    item = FeedItem(source_id=source.id, external_id=external_id)
                    session.add(item)
                    feed_items_by_external_id[external_id] = item

                item.canonical_url = post.get("url")
                item.content_text = post.get("text")
                item.published_at = post.get("date")
                item.views = post.get("views")
                item.forwards = post.get("forwards")
                item.metadata_json = {"media_type": post.get("media_type")}
                item.legacy_channel_post_id = channel_post.id if channel_post else None
                item.updated_at = _utcnow()

    def mirror_posts_to_signal_sources(
        self,
        *,
        user_id: int,
        channel: Any,
        posts: list[dict[str, Any]],
    ) -> None:
        _run_sync(
            self.async_mirror_posts_to_signal_sources(
                user_id=user_id,
                channel=channel,
                posts=posts,
            )
        )

    async def async_get_channel_run_state(
        self,
        *,
        user_id: int,
        channel: Any,
    ) -> dict[str, Any]:
        # Uses transaction() (not session()) deliberately: _ensure_channel_source
        # upserts the backing Source/Subscription rows and syncs channel fields,
        # so this path needs the commit. It is not read-only.
        async with self._database().transaction() as session:
            source = await self._ensure_channel_source(
                session=session,
                user_id=user_id,
                channel=channel,
            )
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == user_id,
                    Subscription.source_id == source.id,
                )
            )
            return self._build_run_state(source, channel, subscription)

    async def async_update_channel_controls(
        self,
        *,
        user_id: int,
        username: str,
        is_active: bool | None = None,
        fetch_interval_seconds: int | None = None,
        max_items_per_run: int | None = None,
        retry_policy: dict[str, Any] | None = None,
    ) -> bool:
        async with self._database().transaction() as session:
            subscription = await session.scalar(
                select(ChannelSubscription)
                .join(Channel)
                .where(
                    ChannelSubscription.user_id == user_id,
                    Channel.username == username,
                )
            )
            if subscription is None:
                return False

            channel = await session.get(Channel, subscription.channel_id)
            if channel is None:
                return False

            source = await self._ensure_channel_source(
                session=session,
                user_id=user_id,
                channel=channel,
            )
            source_subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == user_id,
                    Subscription.source_id == source.id,
                )
            )
            if source_subscription is None:
                source_subscription = Subscription(user_id=user_id, source_id=source.id)
                session.add(source_subscription)

            now = utc_now()
            if is_active is not None:
                channel.is_active = is_active
                subscription.is_active = is_active
                source.is_active = is_active
                source_subscription.is_active = is_active
                if is_active:
                    source_subscription.next_fetch_at = None

            if fetch_interval_seconds is not None:
                source_subscription.cadence_seconds = fetch_interval_seconds
            if max_items_per_run is not None or retry_policy is not None:
                metadata = (
                    dict(source.metadata_json) if isinstance(source.metadata_json, dict) else {}
                )
                controls = dict(metadata.get("controls") or {})
                if max_items_per_run is not None:
                    controls["max_items_per_run"] = max_items_per_run
                if retry_policy is not None:
                    controls["retry_policy"] = retry_policy
                if fetch_interval_seconds is not None:
                    controls["fetch_interval_seconds"] = fetch_interval_seconds
                metadata["controls"] = controls
                source.metadata_json = metadata

            channel.updated_at = now
            subscription.updated_at = now
            source.updated_at = now
            source_subscription.updated_at = now
            return True

    def update_channel_controls(
        self,
        *,
        user_id: int,
        username: str,
        is_active: bool | None = None,
        fetch_interval_seconds: int | None = None,
        max_items_per_run: int | None = None,
        retry_policy: dict[str, Any] | None = None,
    ) -> bool:
        return _run_sync(
            self.async_update_channel_controls(
                user_id=user_id,
                username=username,
                is_active=is_active,
                fetch_interval_seconds=fetch_interval_seconds,
                max_items_per_run=max_items_per_run,
                retry_policy=retry_policy,
            )
        )

    async def async_retry_channel(self, *, user_id: int, username: str) -> bool:
        async with self._database().transaction() as session:
            subscription = await session.scalar(
                select(ChannelSubscription)
                .join(Channel)
                .where(
                    ChannelSubscription.user_id == user_id,
                    Channel.username == username,
                )
            )
            if subscription is None:
                return False
            channel = await session.get(Channel, subscription.channel_id)
            if channel is None:
                return False
            source = await self._ensure_channel_source(
                session=session,
                user_id=user_id,
                channel=channel,
            )
            source_subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == user_id,
                    Subscription.source_id == source.id,
                )
            )
            if source_subscription is None:
                source_subscription = Subscription(user_id=user_id, source_id=source.id)
                session.add(source_subscription)
            now = utc_now()
            channel.is_active = True
            subscription.is_active = True
            source.is_active = True
            source_subscription.is_active = True
            source_subscription.next_fetch_at = None
            channel.updated_at = now
            subscription.updated_at = now
            source.updated_at = now
            source_subscription.updated_at = now
            return True

    def retry_channel(self, *, user_id: int, username: str) -> bool:
        return _run_sync(self.async_retry_channel(user_id=user_id, username=username))

    async def async_update_channel_fetch_success(self, channel: Any) -> None:
        now = utc_now()
        async with self._database().transaction() as session:
            await session.execute(
                update(Channel)
                .where(Channel.id == channel.id)
                .values(
                    last_fetched_at=now,
                    fetch_error_count=0,
                    last_error=None,
                    updated_at=now,
                )
            )

    def update_channel_fetch_success(self, channel: Any) -> None:
        _run_sync(self.async_update_channel_fetch_success(channel))

    async def async_record_channel_fetch_error(
        self, channel: Any, error: str, *, max_errors: int
    ) -> bool:
        new_count = channel.fetch_error_count + 1
        disable: bool = bool(new_count >= max_errors)
        now = utc_now()
        values: dict[str, Any] = {
            "fetch_error_count": Channel.fetch_error_count + 1,
            "last_error": error,
            "updated_at": now,
        }
        if disable:
            values["is_active"] = False
        async with self._database().transaction() as session:
            await session.execute(update(Channel).where(Channel.id == channel.id).values(**values))
            source = await session.scalar(
                select(Source).where(
                    Source.kind == "telegram_channel",
                    Source.external_id == channel.username,
                )
            )
            if source is not None:
                source.fetch_error_count = (source.fetch_error_count or 0) + 1
                source.last_error = error
                source.updated_at = now
                if disable:
                    source.is_active = False
                backoff_seconds = 300 * (2 ** max(0, new_count - 1))
                await session.execute(
                    update(Subscription)
                    .where(Subscription.source_id == source.id)
                    .values(next_fetch_at=now + timedelta(seconds=backoff_seconds), updated_at=now)
                )
        return disable

    def record_channel_fetch_error(self, channel: Any, error: str, *, max_errors: int) -> bool:
        return _run_sync(
            self.async_record_channel_fetch_error(
                channel,
                error,
                max_errors=max_errors,
            )
        )

    async def async_get_channel_post(
        self, *, channel_id: int, message_id: int
    ) -> ChannelPost | None:
        async with self._database().session() as session:
            return await session.scalar(
                select(ChannelPost).where(
                    ChannelPost.channel_id == channel_id,
                    ChannelPost.message_id == message_id,
                )
            )

    def get_channel_post(self, *, channel_id: int, message_id: int) -> Any | None:
        return _run_sync(self.async_get_channel_post(channel_id=channel_id, message_id=message_id))

    async def async_find_cached_analysis(self, post: dict[str, Any]) -> dict[str, Any] | None:
        async with self._database().session() as session:
            # Single LEFT JOIN instead of two sequential SELECTs.
            row = (
                await session.execute(
                    select(ChannelPost, ChannelPostAnalysis)
                    .outerjoin(
                        ChannelPostAnalysis,
                        ChannelPostAnalysis.post_id == ChannelPost.id,
                    )
                    .where(
                        ChannelPost.channel_id == post.get("_channel_id"),
                        ChannelPost.message_id == post["message_id"],
                    )
                )
            ).first()
            if row is None:
                return None
            channel_post, existing = row
            if channel_post and channel_post.analyzed_at and existing:
                return {
                    **post,
                    "real_topic": existing.real_topic,
                    "tldr": existing.tldr,
                    "key_insights": existing.key_insights or [],
                    "relevance_score": existing.relevance_score,
                    "content_type": existing.content_type,
                    "is_ad": False,
                }
            return None

    def find_cached_analysis(self, post: dict[str, Any]) -> dict[str, Any] | None:
        return _run_sync(self.async_find_cached_analysis(post))

    async def async_persist_analysis(self, post: dict[str, Any], fields: dict[str, Any]) -> None:
        async with self._database().transaction() as session:
            channel_post = await session.scalar(
                select(ChannelPost).where(
                    ChannelPost.channel_id == post.get("_channel_id"),
                    ChannelPost.message_id == post["message_id"],
                )
            )
            if channel_post is None:
                return

            existing = await session.scalar(
                select(ChannelPostAnalysis).where(ChannelPostAnalysis.post_id == channel_post.id)
            )
            if existing is None:
                session.add(
                    ChannelPostAnalysis(
                        post_id=channel_post.id,
                        real_topic=fields["real_topic"],
                        tldr=fields["tldr"],
                        key_insights=fields["key_insights"],
                        relevance_score=fields["relevance_score"],
                        content_type=fields["content_type"],
                    )
                )

            await session.execute(
                update(ChannelPost)
                .where(ChannelPost.id == channel_post.id)
                .values(analyzed_at=utc_now())
            )

    def persist_analysis(self, post: dict[str, Any], fields: dict[str, Any]) -> None:
        _run_sync(self.async_persist_analysis(post, fields))

    async def async_create_delivery(
        self,
        *,
        user_id: int,
        post_count: int,
        channel_count: int,
        digest_type: str,
        correlation_id: str,
        post_ids: list[int],
    ) -> None:
        async with self._database().transaction() as session:
            session.add(
                DigestDelivery(
                    user_id=user_id,
                    delivered_at=utc_now(),
                    post_count=post_count,
                    channel_count=channel_count,
                    digest_type=digest_type,
                    correlation_id=correlation_id,
                    posts_json=post_ids,
                )
            )

    def create_delivery(
        self,
        *,
        user_id: int,
        post_count: int,
        channel_count: int,
        digest_type: str,
        correlation_id: str,
        post_ids: list[int],
    ) -> None:
        _run_sync(
            self.async_create_delivery(
                user_id=user_id,
                post_count=post_count,
                channel_count=channel_count,
                digest_type=digest_type,
                correlation_id=correlation_id,
                post_ids=post_ids,
            )
        )

    async def async_get_users_with_subscriptions(self) -> list[int]:
        async with self._database().session() as session:
            rows = (
                await session.execute(
                    select(ChannelSubscription.user_id)
                    .where(ChannelSubscription.is_active.is_(True))
                    .distinct()
                )
            ).scalars()
            return list(rows)

    def get_users_with_subscriptions(self) -> list[int]:
        return _run_sync(self.async_get_users_with_subscriptions())

    async def _ensure_channel_source(
        self,
        *,
        session: Any,
        user_id: int,
        channel: Any,
    ) -> Source:
        persistent_channel = await session.get(Channel, channel.id)
        if persistent_channel is not None:
            channel = persistent_channel
        source = await session.scalar(
            select(Source).where(
                Source.kind == "telegram_channel",
                Source.external_id == channel.username,
            )
        )
        if source is None:
            source = Source(kind="telegram_channel", external_id=channel.username)
            session.add(source)
            await session.flush()

        self._apply_channel_to_source(source, channel)

        subscription = await session.scalar(
            select(Subscription).where(
                Subscription.user_id == user_id,
                Subscription.source_id == source.id,
            )
        )
        if subscription is None:
            session.add(Subscription(user_id=user_id, source_id=source.id, is_active=True))
            await session.flush()
        return cast("Source", source)

    @staticmethod
    def _apply_channel_to_source(source: Source, channel: Any) -> None:
        """Sync mutable Source fields from the Channel row (idempotent)."""
        metadata = dict(source.metadata_json) if isinstance(source.metadata_json, dict) else {}
        controls = metadata.get("controls")
        source.url = f"https://t.me/{channel.username}"
        source.title = channel.title
        source.description = channel.description
        source.is_active = channel.is_active
        source.fetch_error_count = channel.fetch_error_count
        source.last_error = channel.last_error
        source.last_fetched_at = channel.last_fetched_at
        source.legacy_channel_id = channel.id
        source.metadata_json = {
            "channel_id": channel.channel_id,
            "member_count": channel.member_count,
            **({"controls": controls} if isinstance(controls, dict) else {}),
        }
        source.updated_at = _utcnow()

    @staticmethod
    def _build_run_state(source: Source, channel: Any, subscription: Any) -> dict[str, Any]:
        """Build the channel run-state dict from a source + subscription pair."""
        metadata = source.metadata_json if isinstance(source.metadata_json, dict) else {}
        controls = metadata.get("controls") if isinstance(metadata.get("controls"), dict) else {}
        return {
            "is_active": bool(source.is_active and channel.is_active),
            "active_subscription": bool(subscription and subscription.is_active),
            "backoff_until": subscription.next_fetch_at if subscription else None,
            "fetch_interval_seconds": subscription.cadence_seconds if subscription else None,
            "max_items_per_run": _coerce_positive_int(controls.get("max_items_per_run")),
            "retry_policy": controls.get("retry_policy"),
        }

    async def _batch_channel_run_states(
        self,
        session: Any,
        *,
        user_id: int,
        channels: list[Any],
    ) -> dict[int, dict[str, Any]]:
        """Resolve run-state for many channels with O(1) queries.

        Loads/creates the backing Source and Subscription rows for every channel
        in a fixed number of statements (two IN-list SELECTs plus flushes for any
        newly-created rows), instead of one transaction + several SELECTs per
        channel as the per-channel path does.
        """
        if not channels:
            return {}

        usernames = [channel.username for channel in channels]
        existing_sources = (
            (
                await session.execute(
                    select(Source).where(
                        Source.kind == "telegram_channel",
                        Source.external_id.in_(usernames),
                    )
                )
            )
            .scalars()
            .all()
        )
        source_by_username: dict[str, Source] = {s.external_id: s for s in existing_sources}

        for channel in channels:
            source = source_by_username.get(channel.username)
            if source is None:
                source = Source(kind="telegram_channel", external_id=channel.username)
                session.add(source)
                source_by_username[channel.username] = source
            self._apply_channel_to_source(source, channel)
        await session.flush()  # assign ids to any newly-created sources

        source_ids = [source.id for source in source_by_username.values()]
        existing_subs = (
            (
                await session.execute(
                    select(Subscription).where(
                        Subscription.user_id == user_id,
                        Subscription.source_id.in_(source_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        sub_by_source: dict[int, Any] = {s.source_id: s for s in existing_subs}

        for source in source_by_username.values():
            if source.id not in sub_by_source:
                new_sub = Subscription(user_id=user_id, source_id=source.id, is_active=True)
                session.add(new_sub)
                sub_by_source[source.id] = new_sub
        await session.flush()

        run_states: dict[int, dict[str, Any]] = {}
        for channel in channels:
            source = source_by_username[channel.username]
            run_states[channel.id] = self._build_run_state(
                source, channel, sub_by_source.get(source.id)
            )
        return run_states


def _coerce_positive_int(value: Any) -> int | None:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return None
    return coerced if coerced > 0 else None


def _channel_source_due(run_state: dict[str, Any]) -> bool:
    if not run_state.get("is_active", True):
        return False
    if not run_state.get("active_subscription", True):
        return False
    backoff_until = run_state.get("backoff_until")
    return not isinstance(backoff_until, datetime) or backoff_until <= utc_now()
