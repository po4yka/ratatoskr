"""Digest service -- orchestrates reader + analyzer + formatter + delivery."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import get_logger
from app.adapters.content.streaming.operation_streams import (
    digest_run_topic,
    publish_operation_event,
)
from app.infrastructure.persistence.digest_store import DigestStore
from app.observability.metrics_digest import (
    record_digest_delivery,
    record_digest_pipeline_duration,
    record_digest_posts_analyzed,
)

if TYPE_CHECKING:
    from app.adapters.digest.analyzer import DigestAnalyzer
    from app.adapters.digest.channel_reader import ChannelReader
    from app.adapters.digest.formatter import DigestFormatter
    from app.adapters.email.service import EmailDeliveryService
    from app.config import AppConfig

logger = get_logger(__name__)


@dataclass
class DigestResult:
    """Result of a digest generation."""

    user_id: int
    post_count: int = 0
    channel_count: int = 0
    digest_type: str = "on_demand"
    correlation_id: str = ""
    messages_sent: int = 0
    errors: list[str] = field(default_factory=list)


class DigestService:
    """Orchestrates channel reading, analysis, formatting, and delivery."""

    def __init__(
        self,
        cfg: AppConfig,
        reader: ChannelReader,
        analyzer: DigestAnalyzer,
        formatter: DigestFormatter,
        send_message_func: Any,  # async callable(user_id, text, reply_markup=...)
    ) -> None:
        self._cfg = cfg
        self._reader = reader
        self._analyzer = analyzer
        self._formatter = formatter
        self._send = send_message_func
        self._store = DigestStore()
        self._email_service: EmailDeliveryService | None = None

    async def generate_digest(
        self,
        user_id: int,
        correlation_id: str,
        digest_type: str = "on_demand",
        lang: str = "en",
    ) -> DigestResult:
        """Generate and deliver a digest to a user.

        Args:
            user_id: Telegram user ID.
            correlation_id: Correlation ID for tracing.
            digest_type: 'scheduled' or 'on_demand'.
            lang: Language for LLM analysis prompts.

        Returns:
            DigestResult with delivery statistics.
        """
        result = DigestResult(
            user_id=user_id,
            digest_type=digest_type,
            correlation_id=correlation_id,
        )
        started_at = time.monotonic()

        # 1. Fetch posts
        _publish_digest_event(correlation_id, "phase", {"phase": "fetching"})
        try:
            posts = await self._reader.fetch_posts_for_user(user_id)
        except Exception as e:
            logger.exception("digest_fetch_failed", extra={"cid": correlation_id, "uid": user_id})
            result.errors.append(f"Fetch failed: {e}")
            _publish_digest_event(correlation_id, "error", {"phase": "fetching", "message": str(e)})
            _record_digest_outcome(result, "failed", started_at)
            return result

        if not posts:
            logger.info("digest_no_posts", extra={"cid": correlation_id, "uid": user_id})
            try:
                await self._send_user_message(
                    user_id,
                    "\u041d\u0435\u0442 \u043d\u043e\u0432\u044b\u0445 \u043f\u043e\u0441\u0442\u043e\u0432 \u0432 \u043f\u043e\u0434\u043f\u0438\u0441\u0430\u043d\u043d\u044b\u0445 \u043a\u0430\u043d\u0430\u043b\u0430\u0445.",
                    subject="Ratatoskr digest: no new posts",
                    correlation_id=correlation_id,
                )
                result.messages_sent = 1
            except Exception as e:
                result.errors.append(f"Send failed: {e}")
            _publish_digest_event(
                correlation_id,
                "delivered",
                {"messages_sent": result.messages_sent, "status": "empty"},
            )
            _publish_digest_event(
                correlation_id,
                "done" if not result.errors else "error",
                _digest_terminal_payload(result),
            )
            _record_digest_outcome(result, "failed" if result.errors else "empty", started_at)
            return result

        # 2-5. Analyze, filter, format, deliver, persist
        return await self._run_digest_pipeline(posts, result, correlation_id, lang, started_at)

    async def generate_channel_digest(
        self,
        user_id: int,
        channel: Any,
        correlation_id: str,
        lang: str = "en",
    ) -> DigestResult:
        """Generate a digest for a single channel's unread posts.

        Args:
            user_id: Telegram user ID.
            channel: Channel record to digest.
            correlation_id: Correlation ID for tracing.
            lang: Language for LLM analysis prompts.

        Returns:
            DigestResult with delivery statistics.
        """
        result = DigestResult(
            user_id=user_id,
            digest_type="channel_on_demand",
            correlation_id=correlation_id,
        )
        started_at = time.monotonic()

        # 1. Fetch unread posts from the single channel
        _publish_digest_event(
            correlation_id,
            "phase",
            {"phase": "fetching", "channel": getattr(channel, "username", None)},
        )
        try:
            posts = await self._reader.fetch_posts_for_channel(channel, user_id)
        except Exception as e:
            logger.exception(
                "cdigest_fetch_failed",
                extra={"cid": correlation_id, "uid": user_id, "channel": channel.username},
            )
            result.errors.append(f"Fetch failed: {e}")
            _publish_digest_event(correlation_id, "error", {"phase": "fetching", "message": str(e)})
            _record_digest_outcome(result, "failed", started_at)
            return result

        if not posts:
            logger.info(
                "cdigest_no_unread",
                extra={"cid": correlation_id, "uid": user_id, "channel": channel.username},
            )
            try:
                await self._send_user_message(
                    user_id,
                    f"\u041d\u0435\u0442 \u043d\u0435\u043f\u0440\u043e\u0447\u0438\u0442\u0430\u043d\u043d\u044b\u0445 \u043f\u043e\u0441\u0442\u043e\u0432 \u0432 @{channel.username}.",
                    subject=f"Ratatoskr digest: no unread posts in @{channel.username}",
                    correlation_id=correlation_id,
                )
                result.messages_sent = 1
            except Exception as e:
                result.errors.append(f"Send failed: {e}")
            _publish_digest_event(
                correlation_id,
                "delivered",
                {"messages_sent": result.messages_sent, "status": "empty"},
            )
            _publish_digest_event(
                correlation_id,
                "done" if not result.errors else "error",
                _digest_terminal_payload(result),
            )
            _record_digest_outcome(result, "failed" if result.errors else "empty", started_at)
            return result

        # 2-5. Analyze, filter, format, deliver, persist
        return await self._run_digest_pipeline(posts, result, correlation_id, lang, started_at)

    async def _run_digest_pipeline(
        self,
        posts: list[dict[str, Any]],
        result: DigestResult,
        correlation_id: str,
        lang: str,
        started_at: float | None = None,
    ) -> DigestResult:
        """Shared pipeline: analyze, filter, format, deliver, persist."""
        started_at = started_at if started_at is not None else time.monotonic()
        user_id = result.user_id

        # 2. Analyze posts
        _publish_digest_event(
            correlation_id,
            "channel_processed",
            {"channels": _channel_count(posts), "posts": len(posts)},
        )
        _publish_digest_event(correlation_id, "phase", {"phase": "analyzing", "posts": len(posts)})
        try:
            analyzed = await self._analyzer.analyze_posts(posts, correlation_id, lang)
        except Exception as e:
            logger.exception("digest_analysis_failed", extra={"cid": correlation_id})
            result.errors.append(f"Analysis failed: {e}")
            record_digest_posts_analyzed("llm_error", count=len(posts))
            _publish_digest_event(
                correlation_id, "error", {"phase": "analyzing", "message": str(e)}
            )
            _record_digest_outcome(result, "failed", started_at)
            return result

        if not analyzed:
            record_digest_posts_analyzed("skipped", count=len(posts))
            _publish_digest_event(
                correlation_id,
                "posts_analyzed",
                {"input_posts": len(posts), "kept_posts": 0, "status": "empty"},
            )
            await self._send_info_message_or_record_error(
                user_id,
                "\u041f\u043e\u0441\u0442\u044b \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u044b, \u043d\u043e \u0430\u043d\u0430\u043b\u0438\u0437 \u043d\u0435 \u0434\u0430\u043b \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u043e\u0432.",
                result,
            )
            _publish_digest_event(
                correlation_id,
                "delivered",
                {"messages_sent": result.messages_sent, "status": "empty"},
            )
            _publish_digest_event(
                correlation_id,
                "done" if not result.errors else "error",
                _digest_terminal_payload(result),
            )
            _record_digest_outcome(result, "failed" if result.errors else "empty", started_at)
            return result

        # 2b. Filter out ads and announcements
        pre_filter_count = len(analyzed)
        analyzed = [
            p
            for p in analyzed
            if not p.get("is_ad", False) and p.get("content_type") != "announcement"
        ]
        filtered_count = pre_filter_count - len(analyzed)
        if filtered_count:
            logger.info(
                "digest_filtered_posts",
                extra={
                    "cid": correlation_id,
                    "filtered": filtered_count,
                    "remaining": len(analyzed),
                },
            )
            record_digest_posts_analyzed("skipped", count=filtered_count)

        if not analyzed:
            _publish_digest_event(
                correlation_id,
                "posts_analyzed",
                {"input_posts": len(posts), "kept_posts": 0, "status": "filtered"},
            )
            await self._send_info_message_or_record_error(
                user_id,
                "\u0412\u0441\u0435 \u043f\u043e\u0441\u0442\u044b \u043e\u0442\u0444\u0438\u043b\u044c\u0442\u0440\u043e\u0432\u0430\u043d\u044b (\u0440\u0435\u043a\u043b\u0430\u043c\u0430/\u0430\u043d\u043e\u043d\u0441\u044b). \u041d\u0435\u0447\u0435\u0433\u043e \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0442\u044c.",
                result,
            )
            _publish_digest_event(
                correlation_id,
                "delivered",
                {"messages_sent": result.messages_sent, "status": "empty"},
            )
            _publish_digest_event(
                correlation_id,
                "done" if not result.errors else "error",
                _digest_terminal_payload(result),
            )
            _record_digest_outcome(result, "failed" if result.errors else "empty", started_at)
            return result

        # 2c. Cross-channel deduplication by fuzzy topic matching
        pre_dedup_count = len(analyzed)
        analyzed = _deduplicate_posts(analyzed)
        dedup_dropped = pre_dedup_count - len(analyzed)
        if dedup_dropped:
            logger.info(
                "digest_dedup_dropped",
                extra={"cid": correlation_id, "dropped": dedup_dropped},
            )
            record_digest_posts_analyzed("skipped", count=dedup_dropped)

        # 2d. Filter by minimum relevance score
        min_rel = self._cfg.digest.min_relevance_score
        pre_rel_count = len(analyzed)
        analyzed = [p for p in analyzed if p.get("relevance_score", 0) >= min_rel]
        rel_dropped = pre_rel_count - len(analyzed)
        if rel_dropped:
            logger.info(
                "digest_low_relevance_dropped",
                extra={
                    "cid": correlation_id,
                    "dropped": rel_dropped,
                    "threshold": min_rel,
                },
            )
            record_digest_posts_analyzed("skipped", count=rel_dropped)

        if not analyzed:
            _publish_digest_event(
                correlation_id,
                "posts_analyzed",
                {"input_posts": len(posts), "kept_posts": 0, "status": "filtered"},
            )
            await self._send_info_message_or_record_error(
                user_id,
                "\u0412\u0441\u0435 \u043f\u043e\u0441\u0442\u044b \u043e\u0442\u0444\u0438\u043b\u044c\u0442\u0440\u043e\u0432\u0430\u043d\u044b (\u0440\u0435\u043a\u043b\u0430\u043c\u0430, \u0434\u0443\u0431\u043b\u0438 \u0438\u043b\u0438 \u043d\u0438\u0437\u043a\u0430\u044f \u0440\u0435\u043b\u0435\u0432\u0430\u043d\u0442\u043d\u043e\u0441\u0442\u044c).",
                result,
            )
            _publish_digest_event(
                correlation_id,
                "delivered",
                {"messages_sent": result.messages_sent, "status": "empty"},
            )
            _publish_digest_event(
                correlation_id,
                "done" if not result.errors else "error",
                _digest_terminal_payload(result),
            )
            _record_digest_outcome(result, "failed" if result.errors else "empty", started_at)
            return result

        record_digest_posts_analyzed("ok", count=len(analyzed))
        _publish_digest_event(
            correlation_id,
            "posts_analyzed",
            {"input_posts": len(posts), "kept_posts": len(analyzed), "status": "ok"},
        )

        # 3. Format digest
        _publish_digest_event(correlation_id, "phase", {"phase": "formatting"})
        message_chunks = self._formatter.format_digest(analyzed)

        # Count unique channels
        channels_seen = {p.get("_channel_username") for p in analyzed if p.get("_channel_username")}
        result.post_count = len(analyzed)
        result.channel_count = len(channels_seen)

        # 4. Deliver via preferred sink
        _publish_digest_event(
            correlation_id,
            "phase",
            {"phase": "delivering", "chunks": len(message_chunks)},
        )
        try:
            result.messages_sent = await self._deliver_digest_messages(
                user_id=user_id,
                message_chunks=message_chunks,
                digest_type=result.digest_type,
                correlation_id=correlation_id,
                post_count=result.post_count,
                channel_count=result.channel_count,
            )
        except Exception as e:
            logger.warning(
                "digest_send_failed",
                extra={"cid": correlation_id, "error": str(e)},
                exc_info=True,
            )
            result.errors.append(f"Send failed: {e}")

        _publish_digest_event(
            correlation_id,
            "delivered",
            {
                "messages_sent": result.messages_sent,
                "post_count": result.post_count,
                "channel_count": result.channel_count,
            },
        )

        # 5. Persist delivery record
        post_ids = [p.get("message_id") for p in analyzed]
        try:
            await self._store.async_create_delivery(
                user_id=user_id,
                post_count=result.post_count,
                channel_count=result.channel_count,
                digest_type=result.digest_type,
                correlation_id=correlation_id,
                post_ids=post_ids,
            )
        except Exception as exc:
            logger.error(
                "digest_delivery_persist_failed",
                extra={
                    "cid": correlation_id,
                    "uid": user_id,
                    "post_ids_sample": post_ids[:5],
                    "error": str(exc),
                },
            )
            result.errors.append(f"Delivery record not saved: {exc}")

        logger.info(
            "digest_delivered",
            extra={
                "cid": correlation_id,
                "uid": user_id,
                "posts": result.post_count,
                "channels": result.channel_count,
                "messages": result.messages_sent,
                "type": result.digest_type,
            },
        )
        status = "sent" if result.messages_sent > 0 and not result.errors else "failed"
        _publish_digest_event(
            correlation_id,
            "done" if status == "sent" else "error",
            _digest_terminal_payload(result),
        )
        _record_digest_outcome(result, status, started_at)
        return result

    async def _send_info_message_or_record_error(
        self,
        user_id: int,
        text: str,
        result: DigestResult,
    ) -> None:
        """Send a single informational message, recording delivery/send errors."""
        try:
            await self._send_user_message(
                user_id,
                text,
                subject="Ratatoskr digest status",
                correlation_id=result.correlation_id,
            )
            result.messages_sent = 1
        except Exception as e:
            logger.warning(
                "digest_send_info_failed",
                extra={"uid": user_id, "error": str(e)},
                exc_info=True,
            )
            result.errors.append(f"Send failed: {e}")

    async def _deliver_digest_messages(
        self,
        *,
        user_id: int,
        message_chunks: list[tuple[str, list[list[dict[str, str]]]]],
        digest_type: str,
        correlation_id: str,
        post_count: int,
        channel_count: int,
    ) -> int:
        preference = await self._store.async_get_user_preference(user_id)
        if getattr(preference, "delivery_channel", "telegram") == "email":
            text = "\n\n".join(chunk for chunk, _buttons in message_chunks)
            await self._email().send_digest(
                user_id=user_id,
                address_id=getattr(preference, "email_address_id", None),
                subject=f"Ratatoskr {digest_type.replace('_', ' ')} digest",
                text=text,
                correlation_id=correlation_id,
                metadata={"post_count": post_count, "channel_count": channel_count},
            )
            return 1

        sent = 0
        for text, buttons in message_chunks:
            reply_markup = _build_inline_keyboard(buttons) if buttons else None
            await self._send(user_id, text, reply_markup=reply_markup)
            sent += 1
        return sent

    async def _send_user_message(
        self,
        user_id: int,
        text: str,
        *,
        subject: str,
        correlation_id: str,
    ) -> None:
        preference = await self._store.async_get_user_preference(user_id)
        if getattr(preference, "delivery_channel", "telegram") == "email":
            await self._email().send_digest(
                user_id=user_id,
                address_id=getattr(preference, "email_address_id", None),
                subject=subject,
                text=text,
                correlation_id=correlation_id,
                metadata={"kind": "digest_status"},
            )
            return
        await self._send(user_id, text)

    def _email(self) -> EmailDeliveryService:
        if self._email_service is None:
            from app.adapters.email.service import EmailDeliveryService

            self._email_service = EmailDeliveryService(self._cfg.email)
        return self._email_service

    @classmethod
    async def async_get_users_with_subscriptions(cls) -> list[int]:
        """Return user IDs that have at least one active subscription."""
        return await DigestStore().async_get_users_with_subscriptions()

    @classmethod
    def get_users_with_subscriptions(cls) -> list[int]:
        """Return user IDs that have at least one active subscription."""
        return DigestStore().get_users_with_subscriptions()


def _deduplicate_posts(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove cross-channel duplicates by fuzzy topic matching.

    Posts are sorted by relevance desc; the first occurrence of a topic is
    kept, later posts with SequenceMatcher ratio > 0.75 are dropped.
    """
    if len(posts) <= 64:
        return _deduplicate_posts_pairwise(posts)

    return _deduplicate_posts_bucketed(posts)


def _deduplicate_posts_pairwise(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve the original all-pairs fuzzy dedupe for small digests."""
    kept: list[dict[str, Any]] = []
    for post in sorted(posts, key=lambda p: p.get("relevance_score", 0), reverse=True):
        topic = post.get("real_topic", "").lower()
        is_dup = any(
            SequenceMatcher(None, topic, k.get("real_topic", "").lower()).ratio() > 0.75
            for k in kept
        )
        if not is_dup:
            kept.append(post)
    return kept


def _deduplicate_posts_bucketed(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fuzzy-dedupe large digests against likely duplicate buckets only."""
    kept: list[dict[str, Any]] = []
    buckets: dict[str, list[dict[str, Any]]] = {}

    for post in sorted(posts, key=lambda p: p.get("relevance_score", 0), reverse=True):
        topic = str(post.get("real_topic") or "").casefold().strip()
        keys = _topic_bucket_keys(topic)
        candidates_by_id: dict[int, dict[str, Any]] = {}
        for key in keys:
            for candidate in buckets.get(key, []):
                candidates_by_id[id(candidate)] = candidate

        is_dup = any(
            SequenceMatcher(
                None,
                topic,
                str(candidate.get("real_topic") or "").casefold().strip(),
            ).ratio()
            > 0.75
            for candidate in candidates_by_id.values()
        )
        if is_dup:
            continue

        kept.append(post)
        for key in keys:
            buckets.setdefault(key, []).append(post)

    return kept


def _topic_bucket_keys(topic: str) -> set[str]:
    tokens = [token for token in topic.split() if token]
    if not tokens:
        return {""}

    keys = {f"prefix:{topic[:3]}", f"first:{tokens[0]}"}
    if len(tokens) > 1:
        keys.add(f"pair:{tokens[0]}:{tokens[1]}")
    for token in tokens[:5]:
        if len(token) >= 4:
            keys.add(f"token:{token}")
    return keys


def _record_digest_outcome(result: DigestResult, status: str, started_at: float) -> None:
    record_digest_delivery(status)
    record_digest_pipeline_duration(
        digest_type=result.digest_type,
        status=status,
        duration_seconds=time.monotonic() - started_at,
    )


def _publish_digest_event(correlation_id: str, kind: str, payload: dict[str, Any]) -> None:
    publish_operation_event(
        topic=digest_run_topic(correlation_id),
        kind=kind,
        correlation_id=correlation_id,
        payload=payload,
    )


def _digest_terminal_payload(result: DigestResult) -> dict[str, Any]:
    return {
        "post_count": result.post_count,
        "channel_count": result.channel_count,
        "messages_sent": result.messages_sent,
        "errors": result.errors,
    }


def _channel_count(posts: list[dict[str, Any]]) -> int:
    return len({post.get("_channel_username") for post in posts if post.get("_channel_username")})


def _build_inline_keyboard(
    button_rows: list[list[dict[str, str]]],
) -> Any:
    """Build an InlineKeyboardMarkup from button dicts."""
    try:
        from app.adapters.telethon_compat import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = []
        for row in button_rows:
            keyboard.append(
                [
                    InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"])
                    for btn in row
                ]
            )
        return InlineKeyboardMarkup(keyboard)
    except ImportError:
        return None
