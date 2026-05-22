"""Channel reader -- fetches posts and applies round-robin fair distribution."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.core.time_utils import utc_now
from app.infrastructure.persistence.digest_store import DigestStore

if TYPE_CHECKING:
    from app.config import AppConfig

    from .userbot_client import UserbotClient

logger = get_logger(__name__)


class ChannelReader:
    """Fetches posts from subscribed channels with fair distribution."""

    def __init__(self, cfg: AppConfig, userbot: UserbotClient) -> None:
        self._cfg = cfg
        self._userbot = userbot
        self._store = DigestStore()

    async def fetch_posts_for_user(
        self, user_id: int, max_posts: int | None = None
    ) -> list[dict[str, Any]]:
        """Fetch and persist posts from all active subscriptions for a user.

        Uses round-robin fair distribution: each channel gets an equal share,
        then remaining slots are filled from channels with more available posts.

        Args:
            user_id: Telegram user ID.
            max_posts: Override for max posts per digest.

        Returns:
            List of post dicts ready for analysis.
        """
        max_total = max_posts or self._cfg.digest.max_posts_per_digest

        subscriptions = await self._store.async_list_fetchable_subscriptions(user_id)

        if not subscriptions:
            logger.info("digest_no_subscriptions", extra={"uid": user_id})
            return []

        # Fetch posts per channel
        channel_posts: dict[int, list[dict[str, Any]]] = {}
        for sub in subscriptions:
            channel = sub.channel
            try:
                run_state = await self._store.async_get_channel_run_state(
                    user_id=user_id,
                    channel=channel,
                )
                max_items = _max_items_per_run(run_state)
                posts = await self._userbot.fetch_channel_posts(
                    channel.username,
                    hours_lookback=self._cfg.digest.hours_lookback,
                    min_length=self._cfg.digest.min_post_length,
                )
                if max_items is not None:
                    posts = posts[:max_items]
                for p in posts:
                    p["_channel_id"] = channel.channel_id or channel.id
                    p["_channel_username"] = channel.username
                await self._store.async_persist_posts(channel, posts)
                await self._store.async_mirror_posts_to_signal_sources(
                    user_id=user_id,
                    channel=channel,
                    posts=posts,
                )
                await self._store.async_update_channel_fetch_success(channel)
                channel_posts[channel.id] = posts
            except Exception:
                logger.exception(
                    "digest_channel_fetch_error",
                    extra={"channel": channel.username, "uid": user_id},
                )
                max_errors = self._cfg.digest.max_fetch_errors
                disable = await self._store.async_record_channel_fetch_error(
                    channel,
                    "fetch_failed",
                    max_errors=max_errors,
                )
                if disable:
                    logger.warning(
                        "digest_channel_auto_disabled",
                        extra={
                            "channel": channel.username,
                            "error_count": channel.fetch_error_count + 1,
                            "threshold": max_errors,
                        },
                    )
                continue

        if not channel_posts:
            return []

        # Filter out already-delivered posts
        delivered_ids = await self._store.async_list_delivered_message_ids(user_id)
        if delivered_ids:
            channel_posts = {
                ch_id: [p for p in posts if p["message_id"] not in delivered_ids]
                for ch_id, posts in channel_posts.items()
            }
            channel_posts = {k: v for k, v in channel_posts.items() if v}

        if not channel_posts:
            return []

        # Round-robin fair distribution
        return self._fair_distribute(
            channel_posts, max_total, self._cfg.digest.max_posts_per_channel
        )

    async def fetch_posts_for_channel(
        self,
        channel: Any,
        user_id: int,
        max_posts: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch unread posts from a single channel for a user.

        'Unread' means the message_id is not present in any prior
        DigestDelivery.posts_json for this user.

        Args:
            channel: Channel record to fetch from.
            user_id: Telegram user ID (for delivery history lookup).
            max_posts: Override for max posts cap.

        Returns:
            List of unread post dicts, sorted by date desc, capped.
        """
        max_total = max_posts or self._cfg.digest.max_posts_per_digest

        if not channel.is_active:
            logger.warning(
                "cdigest_channel_disabled",
                extra={"channel": channel.username, "uid": user_id},
            )
            return []

        run_state = await self._store.async_get_channel_run_state(user_id=user_id, channel=channel)
        if not _channel_source_due(run_state):
            logger.info(
                "cdigest_channel_backoff_or_disabled",
                extra={"channel": channel.username, "uid": user_id},
            )
            return []
        max_items = _max_items_per_run(run_state)
        try:
            posts = await self._userbot.fetch_channel_posts(
                channel.username,
                hours_lookback=self._cfg.digest.hours_lookback,
                min_length=self._cfg.digest.min_post_length,
            )
        except Exception:
            await self._store.async_record_channel_fetch_error(
                channel,
                "fetch_failed",
                max_errors=self._cfg.digest.max_fetch_errors,
            )
            raise
        if max_items is not None:
            posts = posts[:max_items]
        for p in posts:
            p["_channel_id"] = channel.channel_id or channel.id
            p["_channel_username"] = channel.username
        await self._store.async_persist_posts(channel, posts)
        await self._store.async_mirror_posts_to_signal_sources(
            user_id=user_id,
            channel=channel,
            posts=posts,
        )
        await self._store.async_update_channel_fetch_success(channel)

        # Filter out already-delivered posts
        delivered_ids = await self._store.async_list_delivered_message_ids(user_id)
        unread = [p for p in posts if p["message_id"] not in delivered_ids]

        # Sort by date desc, cap
        unread.sort(key=lambda p: p.get("date") or "", reverse=True)
        return unread[:max_total]

    @staticmethod
    def _fair_distribute(
        channel_posts: dict[int, list[dict[str, Any]]],
        max_total: int,
        max_per_channel: int | None = None,
    ) -> list[dict[str, Any]]:
        """Distribute posts fairly across channels.

        Each channel gets floor(max_total / num_channels) posts, capped by
        ``max_per_channel``. Remaining slots are filled round-robin from
        channels with extras.
        """
        num_channels = len(channel_posts)
        if num_channels == 0:
            return []

        fair_share = max_total // num_channels
        if max_per_channel is not None:
            fair_share = min(fair_share, max_per_channel)

        result: list[dict[str, Any]] = []
        overflow: list[dict[str, Any]] = []

        for _channel_id, posts in channel_posts.items():
            # Sort by date desc (most recent first)
            sorted_posts = sorted(posts, key=lambda p: p.get("date") or "", reverse=True)
            # Cap per channel
            if max_per_channel is not None:
                sorted_posts = sorted_posts[:max_per_channel]
            result.extend(sorted_posts[:fair_share])
            overflow.extend(sorted_posts[fair_share:])

        # Fill remaining slots from overflow
        remaining = max_total - len(result)
        if remaining > 0:
            result.extend(overflow[:remaining])

        return result


def _max_items_per_run(run_state: dict[str, Any]) -> int | None:
    try:
        value = int(run_state.get("max_items_per_run") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _channel_source_due(run_state: dict[str, Any]) -> bool:
    if not run_state.get("is_active", True):
        return False
    if not run_state.get("active_subscription", True):
        return False
    backoff_until = run_state.get("backoff_until")
    return not isinstance(backoff_until, datetime) or backoff_until <= utc_now()
