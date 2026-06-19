"""Trigger and background execution helpers for DigestAPIService."""

from __future__ import annotations

import uuid
from tempfile import gettempdir
from typing import TYPE_CHECKING, Any

from app.api.exceptions import ValidationError
from app.api.models.digest import TriggerDigestResponse
from app.api.services._digest_api_shared import logger, require_enabled
from app.adapters.content.streaming.operation_streams import (
    digest_run_topic,
    publish_operation_event,
)
from app.core.channel_utils import parse_channel_input
from app.infrastructure.persistence.digest_store import DigestStore

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.config.digest import ChannelDigestConfig


class DigestTriggerService:
    """Digest trigger and execution helpers."""

    def __init__(self, cfg: ChannelDigestConfig) -> None:
        self._cfg = cfg
        self._store = DigestStore()

    def trigger_digest(self, user_id: int) -> TriggerDigestResponse:
        require_enabled(self._cfg)
        active = self._store.count_active_subscriptions(user_id)
        if active == 0:
            raise ValidationError("No active channel subscriptions. Subscribe to channels first.")

        correlation_id = str(uuid.uuid4())
        logger.info("digest_triggered_via_api", extra={"uid": user_id, "cid": correlation_id})
        return TriggerDigestResponse(status="queued", correlation_id=correlation_id)

    def trigger_channel_digest(self, user_id: int, raw_channel_username: str) -> dict[str, str]:
        require_enabled(self._cfg)
        channel_username, error = parse_channel_input(str(raw_channel_username or ""))
        if error:
            raise ValidationError(error)

        correlation_id = str(uuid.uuid4())
        logger.info(
            "channel_digest_triggered_via_api",
            extra={"uid": user_id, "channel": channel_username, "cid": correlation_id},
        )
        return {
            "status": "queued",
            "channel": channel_username,
            "correlation_id": correlation_id,
        }

    async def execute_digest_trigger(
        self,
        *,
        user_id: int,
        correlation_id: str,
        run_digest_task: Callable[..., Awaitable[Any]],
    ) -> None:
        try:
            result = await run_digest_task(
                user_id=user_id,
                correlation_id=correlation_id,
                channel_username=None,
            )
            logger.info(
                "digest_api_job_complete",
                extra={
                    "uid": user_id,
                    "cid": correlation_id,
                    "posts": result.post_count,
                    "channels": result.channel_count,
                    "messages": result.messages_sent,
                    "errors": len(result.errors),
                },
            )
        except Exception:
            logger.exception("digest_api_job_failed", extra={"uid": user_id, "cid": correlation_id})
            publish_operation_event(
                topic=digest_run_topic(correlation_id),
                kind="error",
                correlation_id=correlation_id,
                payload={"phase": "failed", "message": "digest job failed"},
            )

    async def execute_channel_digest_trigger(
        self,
        *,
        user_id: int,
        correlation_id: str,
        channel_username: str,
        run_digest_task: Callable[..., Awaitable[Any]],
    ) -> None:
        try:
            result = await run_digest_task(
                user_id=user_id,
                correlation_id=correlation_id,
                channel_username=channel_username,
            )
            logger.info(
                "channel_digest_api_job_complete",
                extra={
                    "uid": user_id,
                    "channel": channel_username,
                    "cid": correlation_id,
                    "posts": result.post_count,
                    "channels": result.channel_count,
                    "messages": result.messages_sent,
                    "errors": len(result.errors),
                },
            )
        except Exception:
            logger.exception(
                "channel_digest_api_job_failed",
                extra={"uid": user_id, "channel": channel_username, "cid": correlation_id},
            )
            publish_operation_event(
                topic=digest_run_topic(correlation_id),
                kind="error",
                correlation_id=correlation_id,
                payload={
                    "phase": "failed",
                    "channel": channel_username,
                    "message": "digest job failed",
                },
            )

    async def run_digest_task(
        self,
        *,
        user_id: int,
        correlation_id: str,
        channel_username: str | None,
    ) -> Any:
        """Build runtime dependencies and execute a digest task."""
        from pathlib import Path

        from app.adapters.digest.analyzer import DigestAnalyzer
        from app.adapters.digest.channel_reader import ChannelReader
        from app.adapters.digest.digest_service import DigestService
        from app.adapters.digest.formatter import DigestFormatter
        from app.adapters.digest.userbot_client import UserbotClient
        from app.adapters.openrouter.openrouter_client import OpenRouterClient
        from app.adapters.telegram.telethon_compat import TelethonBotClient
        from app.config import load_config

        session_dir = Path("/data")
        app_cfg = load_config()
        userbot = UserbotClient(app_cfg, session_dir)
        llm_client: OpenRouterClient | None = None

        await userbot.start()
        try:
            llm_client = OpenRouterClient(
                api_key=app_cfg.openrouter.api_key,
                model=app_cfg.openrouter.model,
                fallback_models=app_cfg.openrouter.fallback_models,
            )
            reader = ChannelReader(app_cfg, userbot)
            analyzer = DigestAnalyzer(app_cfg, llm_client)
            formatter = DigestFormatter()

            bot = TelethonBotClient(
                name=f"digest_api_sender_{correlation_id[:8]}",
                api_id=app_cfg.telegram.api_id,
                api_hash=app_cfg.telegram.api_hash,
                bot_token=app_cfg.telegram.bot_token,
                session_dir=gettempdir(),
            )

            async with bot:

                async def _send_message(
                    target_user_id: int,
                    text: str,
                    reply_markup: Any = None,
                ) -> None:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                        reply_markup=reply_markup,
                    )

                service = DigestService(
                    cfg=app_cfg,
                    reader=reader,
                    analyzer=analyzer,
                    formatter=formatter,
                    send_message_func=_send_message,
                )

                if channel_username is None:
                    return await service.generate_digest(
                        user_id=user_id,
                        correlation_id=correlation_id,
                        digest_type="on_demand",
                        lang="ru",
                    )

                channel = await self._store.async_get_or_create_channel(
                    channel_username, title=channel_username
                )
                return await service.generate_channel_digest(
                    user_id=user_id,
                    channel=channel,
                    correlation_id=correlation_id,
                    lang="ru",
                )
        finally:
            if llm_client is not None:
                await llm_client.aclose()
            await userbot.stop()
