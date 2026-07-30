"""Time-window coalescer for consecutive Telegram messages.

When the same user sends several messages within a short idle window, treat
them as parts of a single logical post: feed all parts to
``MultiSourceAggregationService`` and return one synthesized response instead
of N independent summaries.

Single buffered messages flush through the existing single-message router so
the common case is unchanged.

See ``docs/explanation/architecture-overview.md`` for placement in the
pipeline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.logging_utils import generate_correlation_id, get_logger
from app.utils.typing_indicator import TypingIndicator

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.callback_handler import CallbackHandler
    from app.adapters.telegram.multi_source_aggregation_handler import (
        MultiSourceAggregationHandler,
    )
    from app.adapters.telegram.routing.content_router import MessageContentRouter
    from app.adapters.telegram.routing.models import PreparedRouteContext
    from app.adapters.telegram.routing.rate_limit import MessageRateLimitCoordinator
    from app.adapters.telegram.url_handler import URLHandler
    from app.application.dto.aggregation import SourceSubmission

logger = get_logger(__name__)

BucketKey = tuple[int, int]


@dataclass(slots=True)
class _BufferedMessage:
    message: Any
    prepared: PreparedRouteContext
    interaction_id: int
    correlation_id: str
    start_time: float


@dataclass(slots=True)
class _Bucket:
    messages: list[_BufferedMessage] = field(default_factory=list)
    timer_task: asyncio.Task[None] | None = None
    typing: TypingIndicator | None = None
    flushing: bool = False


class MessageCoalescer:
    """Buffer consecutive eligible messages from the same chat and flush them
    together once the user stops typing for the configured idle window.

    Eligibility (must all hold to buffer):
      * text does not start with ``/``;
      * no ``contact`` or ``web_app_data`` payload;
      * no Telegram-native ``media_group_id`` (albums use their own collector);
      * no follow-up reply is pending for this user;
      * the user is not awaiting a URL prompt;
      * coalescing is enabled in runtime config.
    """

    def __init__(
        self,
        *,
        window_sec: float,
        enabled: bool,
        content_router: MessageContentRouter,
        aggregation_handler: MultiSourceAggregationHandler | None,
        rate_limit_coordinator: MessageRateLimitCoordinator,
        response_formatter: ResponseFormatter,
        callback_handler: CallbackHandler | None,
        url_handler: URLHandler | None,
        send_chat_action: Callable[[int, str], Awaitable[bool]] | None,
    ) -> None:
        self._window_sec = window_sec
        self._enabled = enabled
        self._content_router = content_router
        self._aggregation_handler = aggregation_handler
        self._rate_limit_coordinator = rate_limit_coordinator
        self._response_formatter = response_formatter
        self._callback_handler = callback_handler
        self._url_handler = url_handler
        self._send_chat_action = send_chat_action
        self._buckets: dict[BucketKey, _Bucket] = {}
        self._lock = asyncio.Lock()
        self._shutting_down = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def try_buffer(
        self,
        *,
        prepared: PreparedRouteContext,
        message: Any,
        interaction_id: int,
        correlation_id: str,
        start_time: float,
    ) -> bool:
        """Return True if the message was buffered; False if it should be
        routed inline as today."""
        if not self._enabled or self._shutting_down:
            return False
        if prepared.chat_id is None:
            return False
        if not await self._is_eligible(prepared, message):
            return False

        key: BucketKey = (prepared.uid, prepared.chat_id)
        buffered = _BufferedMessage(
            message=message,
            prepared=prepared,
            interaction_id=interaction_id,
            correlation_id=correlation_id,
            start_time=start_time,
        )
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or bucket.flushing:
                bucket = _Bucket()
                self._buckets[key] = bucket
            bucket.messages.append(buffered)
            await self._ensure_typing(bucket, prepared.chat_id)
            self._reset_timer(bucket, key)
        logger.info(
            "coalesce_buffered",
            extra={
                "cid": correlation_id,
                "uid": prepared.uid,
                "chat_id": prepared.chat_id,
                "count": len(bucket.messages),
                "window_sec": self._window_sec,
            },
        )
        return True

    async def flush_now(self, uid: int, chat_id: int | None) -> None:
        """Synchronously drain whatever is buffered for ``(uid, chat_id)``.

        Used by the command path so a /command waits for buffered messages
        to dispatch before the command itself runs.
        """
        if chat_id is None:
            return
        key: BucketKey = (uid, chat_id)
        bucket = await self._claim_bucket(key)
        if bucket is None:
            return
        await self._dispatch(key, bucket)

    async def shutdown(self) -> None:
        """Drain all open buckets — invoked from bot shutdown."""
        self._shutting_down = True
        async with self._lock:
            keys = list(self._buckets.keys())
        for key in keys:
            bucket = await self._claim_bucket(key)
            if bucket is None:
                continue
            try:
                await self._dispatch(key, bucket)
            except Exception:
                logger.exception("coalesce_shutdown_flush_failed", extra={"key": key})

    # ------------------------------------------------------------------ #
    # eligibility / bucket plumbing                                       #
    # ------------------------------------------------------------------ #

    async def _is_eligible(self, prepared: PreparedRouteContext, message: Any) -> bool:
        text = prepared.text or ""
        if text.startswith("/"):
            return False
        if getattr(message, "contact", None) is not None:
            return False
        if getattr(message, "web_app_data", None) is not None:
            return False
        if getattr(message, "media_group_id", None) is not None:
            return False
        if self._callback_handler is not None:
            try:
                if await self._callback_handler.has_pending_followup(prepared.uid):
                    return False
            except Exception:
                logger.debug(
                    "coalesce_followup_probe_failed",
                    extra={"uid": prepared.uid},
                    exc_info=True,
                )
        if self._url_handler is not None:
            try:
                if await self._url_handler.is_awaiting_url(prepared.uid):
                    return False
            except Exception:
                logger.debug(
                    "coalesce_awaited_url_probe_failed",
                    extra={"uid": prepared.uid},
                    exc_info=True,
                )
        return True

    def _reset_timer(self, bucket: _Bucket, key: BucketKey) -> None:
        if bucket.timer_task is not None and not bucket.timer_task.done():
            bucket.timer_task.cancel()
        bucket.timer_task = asyncio.create_task(self._sleep_and_flush(key))

    async def _sleep_and_flush(self, key: BucketKey) -> None:
        try:
            await asyncio.sleep(self._window_sec)
        except asyncio.CancelledError:
            return
        bucket = await self._claim_bucket(key)
        if bucket is None:
            return
        try:
            await self._dispatch(key, bucket)
        except Exception:
            logger.exception("coalesce_timer_flush_failed", extra={"key": key})

    async def _claim_bucket(self, key: BucketKey) -> _Bucket | None:
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or not bucket.messages or bucket.flushing:
                return None
            bucket.flushing = True
            # Cancel any pending timer EXCEPT when this method is called from
            # within that timer task itself (the natural-fire case). Cancelling
            # the current task would raise CancelledError out of this function.
            current = asyncio.current_task()
            if (
                bucket.timer_task is not None
                and not bucket.timer_task.done()
                and bucket.timer_task is not current
            ):
                bucket.timer_task.cancel()
            bucket.timer_task = None
            # Detach the bucket from the dict so a concurrent arrival starts a fresh one
            self._buckets.pop(key, None)
            return bucket

    async def _ensure_typing(self, bucket: _Bucket, chat_id: int) -> None:
        if bucket.typing is not None or self._send_chat_action is None:
            return
        indicator = TypingIndicator(
            send_chat_action_func=self._send_chat_action,
            chat_id=chat_id,
        )
        try:
            await indicator.start()
        except Exception:
            logger.debug(
                "coalesce_typing_start_failed",
                extra={"chat_id": chat_id},
                exc_info=True,
            )
            return
        bucket.typing = indicator

    async def _stop_typing(self, bucket: _Bucket) -> None:
        if bucket.typing is None:
            return
        try:
            await bucket.typing.stop()
        except Exception:
            logger.debug("coalesce_typing_stop_failed", exc_info=True)
        bucket.typing = None

    # ------------------------------------------------------------------ #
    # dispatch                                                            #
    # ------------------------------------------------------------------ #

    async def _dispatch(self, key: BucketKey, bucket: _Bucket) -> None:
        messages = bucket.messages
        await self._stop_typing(bucket)
        if not messages:
            return
        uid, chat_id = key
        if len(messages) == 1:
            await self._dispatch_single(messages[0], uid)
            return
        await self._dispatch_bundle(messages, uid=uid, chat_id=chat_id)

    async def _reject_for_concurrency(
        self,
        buffered: _BufferedMessage,
        uid: int,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Tell the user the concurrency limit refused this work, and record it.

        Mirrors the inline path in ``message_router.route_message``.
        """
        await self._rate_limit_coordinator.handle_concurrent_limit_rejection(
            message=buffered.message,
            uid=uid,
            interaction_type=buffered.prepared.interaction_type,
            correlation_id=correlation_id or buffered.correlation_id,
            interaction_id=buffered.interaction_id,
            start_time=buffered.start_time,
        )

    async def _dispatch_single(self, buffered: _BufferedMessage, uid: int) -> None:
        limiter = await self._rate_limit_coordinator.get_active_limiter()
        slot_acquired = await self._rate_limit_coordinator.acquire_concurrent_slot(limiter, uid)
        if not slot_acquired:
            # This slot is the only thing enforcing RATE_LIMIT_MAX_CONCURRENT on
            # the coalesced path, which is the default route for every
            # non-command message: message_router returns as soon as try_buffer
            # accepts, before reaching its own inline slot check. Routing anyway
            # on a refusal let an unbounded number of scraper+LLM flows run at
            # once, with no reply to the user and no `concurrent_limited` record.
            await self._reject_for_concurrency(buffered, uid)
            return
        try:
            await self._content_router.route(
                buffered.prepared,
                buffered.interaction_id,
                buffered.start_time,
            )
        except Exception:
            logger.exception(
                "coalesce_single_dispatch_failed",
                extra={"cid": buffered.correlation_id, "uid": uid},
            )
        finally:
            if slot_acquired:
                await self._rate_limit_coordinator.release_concurrent_slot(limiter, uid)

    async def _dispatch_bundle(
        self,
        messages: list[_BufferedMessage],
        *,
        uid: int,
        chat_id: int,
    ) -> None:
        first = messages[0]
        bundle_cid = generate_correlation_id()
        per_message_cids = [m.correlation_id for m in messages]
        message_ids = [m.prepared.message_id for m in messages]
        logger.info(
            "coalesce_bundle_dispatch",
            extra={
                "cid": bundle_cid,
                "uid": uid,
                "chat_id": chat_id,
                "count": len(messages),
                "per_message_cids": per_message_cids,
                "window_sec": self._window_sec,
            },
        )

        if self._aggregation_handler is None or not await self._aggregation_handler.ensure_enabled(
            uid=uid, message=first.message, explicit=False
        ):
            await self._dispatch_each_independently(messages, uid)
            return

        submissions = await self._collect_submissions(messages)
        if len(submissions) < 2:
            # Nothing extractable from any message — fall back to per-message
            await self._dispatch_each_independently(messages, uid)
            return

        limiter = await self._rate_limit_coordinator.get_active_limiter()
        slot_acquired = await self._rate_limit_coordinator.acquire_concurrent_slot(limiter, uid)
        if not slot_acquired:
            await self._reject_for_concurrency(first, uid, correlation_id=bundle_cid)
            return
        try:
            await self._aggregation_handler.run_with_submissions(
                message=first.message,
                uid=uid,
                correlation_id=bundle_cid,
                submissions=submissions,
                metadata={
                    "entrypoint": "telegram_coalesced",
                    "window_sec": self._window_sec,
                    "buffered_message_ids": message_ids,
                    "per_message_correlation_ids": per_message_cids,
                    "interaction_ids": [m.interaction_id for m in messages],
                },
            )
        except Exception:
            logger.exception(
                "coalesce_bundle_dispatch_failed",
                extra={"cid": bundle_cid, "uid": uid, "count": len(messages)},
            )
        finally:
            if slot_acquired:
                await self._rate_limit_coordinator.release_concurrent_slot(limiter, uid)

    async def _collect_submissions(
        self, messages: list[_BufferedMessage]
    ) -> list[SourceSubmission]:
        # Downstream extraction agent dedupes by source stable_id
        # (see app/agents/multi_source_extraction_agent.py duplicate_positions),
        # so just hand it the union of submissions in arrival order.
        if self._aggregation_handler is None:
            return []
        collected: list[SourceSubmission] = []
        for buffered in messages:
            try:
                submissions = await self._aggregation_handler.build_submissions_for_message(
                    message=buffered.message,
                    text=buffered.prepared.text or "",
                )
            except Exception:
                logger.exception(
                    "coalesce_build_submissions_failed",
                    extra={"cid": buffered.correlation_id},
                )
                continue
            collected.extend(submissions)
        return collected

    async def _dispatch_each_independently(
        self, messages: list[_BufferedMessage], uid: int
    ) -> None:
        for buffered in messages:
            await self._dispatch_single(buffered, uid)
