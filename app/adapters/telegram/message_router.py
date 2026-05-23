"""Message routing and coordination for Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from app.adapters.telegram.coalescer import MessageCoalescer
from app.adapters.telegram.routing import (
    MessageContentRouter,
    MessageInteractionRecorder,
    MessageRateLimitCoordinator,
    MessageRouteContextBuilder,
    MessageRouteFailureHandler,
)
from app.core.logging_utils import generate_correlation_id
from app.utils.typing_indicator import TypingIndicator

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.attachment.attachment_processor import AttachmentProcessor
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.access_controller import AccessController
    from app.adapters.telegram.callback_handler import CallbackHandler
    from app.adapters.telegram.command_dispatcher import TelegramCommandDispatcher
    from app.adapters.telegram.forward_processor import ForwardProcessor
    from app.adapters.telegram.multi_source_aggregation_handler import (
        MultiSourceAggregationHandler,
    )
    from app.adapters.telegram.routing.models import PreparedRouteContext
    from app.adapters.telegram.routing.voice_message_processor import VoiceMessageProcessor
    from app.adapters.telegram.task_manager import UserTaskManager
    from app.adapters.telegram.url_handler import URLHandler
    from app.application.ports.users import UserRepositoryPort
    from app.config import AppConfig
    from app.db.session import Database
    from app.security.rate_limiter import RedisUserRateLimiter, UserRateLimiter

logger = logging.getLogger("app.adapters.telegram.message_router")


class _NullUserRepository:
    async def async_insert_user_interaction(self, **_kwargs: object) -> int:
        return 0

    async def async_update_user_interaction(self, **_kwargs: object) -> None:
        return None


class MessageRouter:
    """Coordinate explicit Telegram routing collaborators."""

    def __init__(
        self,
        cfg: AppConfig,
        access_controller: AccessController,
        command_processor: TelegramCommandDispatcher,
        url_handler: URLHandler,
        forward_processor: ForwardProcessor,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict], None],
        task_manager: UserTaskManager | None = None,
        attachment_processor: AttachmentProcessor | None = None,
        aggregation_handler: MultiSourceAggregationHandler | None = None,
        user_repo: UserRepositoryPort | None = None,
        callback_handler: CallbackHandler | None = None,
        voice_processor: VoiceMessageProcessor | None = None,
        lang: str = "en",
        db: Database | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.access_controller = access_controller
        self.response_formatter = response_formatter
        self._task_manager = task_manager

        self.user_repo = user_repo or _NullUserRepository()
        self._interaction_recorder = MessageInteractionRecorder(
            self.user_repo,
            structured_output_enabled=cfg.openrouter.enable_structured_outputs,
        )
        self._rate_limit_coordinator = MessageRateLimitCoordinator(
            cfg=cfg,
            response_formatter=response_formatter,
            interaction_recorder=self._interaction_recorder,
            lang=lang,
        )
        self._context_builder = MessageRouteContextBuilder(
            response_formatter=response_formatter,
            recent_message_ids=self._rate_limit_coordinator.recent_message_ids,
            recent_message_ttl=self._rate_limit_coordinator.recent_message_ttl,
        )
        self._content_router = MessageContentRouter(
            command_dispatcher=command_processor,
            url_handler=url_handler,
            forward_processor=forward_processor,
            response_formatter=response_formatter,
            interaction_recorder=self._interaction_recorder,
            callback_handler=callback_handler,
            attachment_processor=attachment_processor,
            aggregation_handler=aggregation_handler,
            voice_processor=voice_processor,
            lang=lang,
            aggregation_default_mode=cfg.runtime.aggregation_default_mode,
            forward_link_bundle_prose_threshold=(cfg.runtime.forward_link_bundle_prose_threshold),
        )
        self._coalescer = MessageCoalescer(
            window_sec=cfg.runtime.aggregate_coalesce_window_sec,
            enabled=cfg.runtime.aggregate_coalesce_enabled,
            content_router=self._content_router,
            aggregation_handler=aggregation_handler,
            rate_limit_coordinator=self._rate_limit_coordinator,
            response_formatter=response_formatter,
            callback_handler=callback_handler,
            url_handler=url_handler,
            send_chat_action=getattr(response_formatter, "send_chat_action", None),
        )
        self._content_router.set_coalescer(self._coalescer)
        self._failure_handler = MessageRouteFailureHandler(
            response_formatter=response_formatter,
            audit_func=audit_func,
            interaction_recorder=self._interaction_recorder,
        )

    @property
    def coalescer(self) -> MessageCoalescer:
        """Public accessor — wired into bot shutdown to drain pending buffers."""
        return self._coalescer

    @property
    def callback_handler(self) -> CallbackHandler | None:
        """Public read-only accessor for the wired callback handler."""
        return self._content_router.callback_handler

    async def route_message(self, message: object) -> None:
        """Main message routing entry point."""
        start_time = time.time()
        interaction_id = 0
        uid = 0
        limiter: RedisUserRateLimiter | UserRateLimiter = self._rate_limit_coordinator.rate_limiter
        concurrent_acquired = False
        correlation_id = generate_correlation_id()
        typing_indicator_obj: TypingIndicator | None = None
        from app.observability.otel import get_tracer, set_correlation_id_attr

        _tracer = get_tracer(__name__)

        try:
            with _tracer.start_as_current_span(
                "telegram.update",
                attributes={
                    "ratatoskr.correlation_id": correlation_id,
                },
            ):
                set_correlation_id_attr(correlation_id)
                route_context = await self._context_builder.prepare(message, correlation_id)
                if route_context is None:
                    return

                uid = route_context.uid

                logger.info(
                    "checking_access_for_user",
                    extra={
                        "cid": correlation_id,
                        "user_id": uid,
                        "user_id_type": type(uid).__name__,
                    },
                )
                if not await self.access_controller.check_access(
                    uid,
                    message,
                    correlation_id,
                    0,
                    start_time,
                ):
                    return

                interaction_id = await self._interaction_recorder.log(route_context)

                limiter = await self._rate_limit_coordinator.get_active_limiter()
                allowed, error_msg = await self._rate_limit_coordinator.check_rate_limit(
                    limiter,
                    uid,
                    route_context.interaction_type,
                )
                if not allowed:
                    await self._rate_limit_coordinator.handle_rate_limit_rejection(
                        message=message,
                        uid=uid,
                        interaction_type=route_context.interaction_type,
                        correlation_id=correlation_id,
                        error_msg=error_msg,
                        interaction_id=interaction_id,
                        start_time=start_time,
                    )
                    return

                if await self._coalescer.try_buffer(
                    prepared=route_context,
                    message=message,
                    interaction_id=interaction_id,
                    correlation_id=correlation_id,
                    start_time=start_time,
                ):
                    # Buffered for time-window coalescing; the flush task will
                    # acquire its own slot and typing indicator when it fires.
                    return

                if not await self._rate_limit_coordinator.acquire_concurrent_slot(limiter, uid):
                    await self._rate_limit_coordinator.handle_concurrent_limit_rejection(
                        message=message,
                        uid=uid,
                        interaction_type=route_context.interaction_type,
                        correlation_id=correlation_id,
                        interaction_id=interaction_id,
                        start_time=start_time,
                    )
                    return
                concurrent_acquired = True

                # Show the user a typing indicator the moment a content-bearing
                # message arrives, so the wait through scraping / link
                # enrichment / LLM cascade is visible. TypingIndicator.start()
                # sends the first action synchronously and then refreshes every
                # 4 s for as long as the request runs. Commands answer
                # instantly and plain text gets the fallback hint, so neither
                # needs an indicator.
                send_chat_action = getattr(self.response_formatter, "send_chat_action", None)
                if (
                    send_chat_action is not None
                    and route_context.chat_id is not None
                    and (
                        route_context.interaction_type == "forward"
                        or route_context.first_url is not None
                    )
                ):
                    typing_indicator_obj = TypingIndicator(
                        send_chat_action_func=send_chat_action,
                        chat_id=route_context.chat_id,
                    )
                    await typing_indicator_obj.start()

                await self._route_content_with_tracking(route_context, interaction_id, start_time)

        except asyncio.CancelledError:
            await self._failure_handler.handle_cancelled(
                correlation_id=correlation_id,
                uid=uid,
                interaction_id=interaction_id,
                start_time=start_time,
            )
            return
        except Exception as exc:
            await self._failure_handler.handle_exception(
                message=message,
                error=exc,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                start_time=start_time,
            )
        finally:
            if typing_indicator_obj is not None:
                try:
                    await typing_indicator_obj.stop()
                except Exception:  # pragma: no cover - defensive: never block teardown
                    logger.debug(
                        "typing_indicator_stop_failed_in_router",
                        extra={"cid": correlation_id},
                        exc_info=True,
                    )
            if concurrent_acquired:
                await self._rate_limit_coordinator.release_concurrent_slot(limiter, uid)

    async def cleanup_rate_limiter(self) -> int:
        """Clean up limiter state held by the rate-limit coordinator."""
        return await self._rate_limit_coordinator.cleanup()

    async def _route_content_with_tracking(
        self,
        route_context: PreparedRouteContext,
        interaction_id: int,
        start_time: float,
    ) -> None:
        text = route_context.text
        uid = route_context.uid

        if self._task_manager is not None:
            async with self._task_manager.track(uid, enabled=not text.startswith("/cancel")):
                await self._content_router.route(route_context, interaction_id, start_time)
            return

        await self._content_router.route(route_context, interaction_id, start_time)
