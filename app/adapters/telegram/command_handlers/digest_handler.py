"""Channel digest command handlers (/digest, /channels, /subscribe, /unsubscribe)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.adapters.telegram.command_handlers.base_handler import HandlerDependenciesMixin
from app.core.channel_utils import parse_channel_input
from app.core.logging_utils import get_logger
from app.infrastructure.persistence.digest_store import DigestStore
from app.infrastructure.persistence.digest_subscription_ops import (
    async_subscribe_channel_atomic,
    async_unsubscribe_channel_atomic,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    from app.adapters.digest.digest_service import DigestService
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)


class DigestHandler(HandlerDependenciesMixin):
    """Implementation of channel digest commands.

    ``digest_service_factory`` is an async-context-manager factory injected at
    construction by the DI layer.  It constructs the full digest stack
    (UserbotClient, ChannelReader, DigestAnalyzer, DigestService) and yields a
    ready-to-use ``DigestService``.  Keeping the factory outside this module
    avoids a runtime cross-adapter import from ``telegram`` into ``digest``.
    """

    _store = DigestStore()

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        response_formatter: ResponseFormatter,
        digest_service_factory: Callable[
            [CommandExecutionContext], AbstractAsyncContextManager[DigestService]
        ]
        | None = None,
    ) -> None:
        super().__init__(cfg=cfg, db=db, response_formatter=response_formatter)
        self._digest_service_factory = digest_service_factory

    @asynccontextmanager
    async def _digest_context(self, ctx: CommandExecutionContext) -> AsyncIterator[DigestService]:
        """Shared setup/teardown for digest commands via the injected factory."""
        if self._digest_service_factory is None:
            raise RuntimeError(
                "DigestHandler requires a digest_service_factory; "
                "wire it up in the DI layer (app/di/telegram_commands.py)."
            )
        async with self._digest_service_factory(ctx) as service:
            yield service

    async def handle_digest(self, ctx: CommandExecutionContext) -> None:
        """Handle /digest command -- generate on-demand digest."""
        if not self._cfg.digest.enabled:
            await self._formatter.safe_reply(
                ctx.message,
                "Channel digest is not enabled.\n\nSet `DIGEST_ENABLED=true` in your environment.",
            )
            return

        await self._formatter.safe_reply(ctx.message, "Generating digest...")

        try:
            async with self._digest_context(ctx) as service:
                result = await service.generate_digest(
                    user_id=ctx.uid,
                    correlation_id=ctx.correlation_id,
                    digest_type="on_demand",
                    lang="ru",
                )
                if result.errors:
                    errors_text = "\n".join(result.errors[:3])
                    await self._formatter.safe_reply(
                        ctx.message,
                        f"Digest completed with errors:\n{errors_text}",
                    )
        except FileNotFoundError:
            await self._formatter.safe_reply(
                ctx.message,
                "Userbot session not found.\n\nRun /init_session first to authenticate.",
            )
        except Exception as exc:
            logger.exception("digest_command_failed", extra={"cid": ctx.correlation_id})
            await self._formatter.safe_reply(
                ctx.message, f"Digest failed: {exc}\nError ID: {ctx.correlation_id}"
            )

    async def handle_cdigest(self, ctx: CommandExecutionContext) -> None:
        """Handle /cdigest @channel_name -- single-channel unread digest."""
        if not self._cfg.digest.enabled:
            await self._formatter.safe_reply(
                ctx.message,
                "Channel digest is not enabled.\n\nSet `DIGEST_ENABLED=true` in your environment.",
            )
            return

        # Parse channel name
        parts = ctx.text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await self._formatter.safe_reply(
                ctx.message,
                "Usage: `/cdigest @channel_name`",
            )
            return

        username, error = parse_channel_input(parts[1])
        if error:
            await self._formatter.safe_reply(ctx.message, error)
            return

        # Get or create channel record (no subscription required)
        channel = await self._store.async_get_or_create_channel(username, title=username)

        await self._formatter.safe_reply(ctx.message, f"Generating digest for @{username}...")

        try:
            async with self._digest_context(ctx) as service:
                result = await service.generate_channel_digest(
                    user_id=ctx.uid,
                    channel=channel,
                    correlation_id=ctx.correlation_id,
                    lang="ru",
                )
                if result.errors:
                    errors_text = "\n".join(result.errors[:3])
                    await self._formatter.safe_reply(
                        ctx.message,
                        f"Digest completed with errors:\n{errors_text}",
                    )
        except FileNotFoundError:
            await self._formatter.safe_reply(
                ctx.message,
                "Userbot session not found.\n\nRun /init_session first to authenticate.",
            )
        except Exception as exc:
            logger.exception("cdigest_command_failed", extra={"cid": ctx.correlation_id})
            await self._formatter.safe_reply(
                ctx.message, f"Channel digest failed: {exc}\nError ID: {ctx.correlation_id}"
            )

    async def handle_channels(self, ctx: CommandExecutionContext) -> None:
        """Handle /channels command -- list subscribed channels."""
        if not self._cfg.digest.enabled:
            await self._formatter.safe_reply(
                ctx.message,
                "Channel digest is not enabled.",
            )
            return

        subs = await self._store.async_list_active_subscriptions(ctx.uid)

        if not subs:
            await self._formatter.safe_reply(
                ctx.message,
                "No channel subscriptions.\n\nUse `/subscribe @channel_name` to add a channel.",
            )
            return

        lines = ["**Subscribed Channels:**\n"]
        for sub in subs:
            ch = sub.channel
            status = "active" if ch.is_active else "paused"
            error_info = f" (errors: {ch.fetch_error_count})" if ch.fetch_error_count else ""
            lines.append(f"  @{ch.username} [{status}]{error_info}")

        lines.append(f"\nTotal subscribed channels: {len(subs)}")

        # Warn about disabled channels the user is subscribed to
        disabled = [s for s in subs if not s.channel.is_active]
        if disabled:
            lines.append("\n**Disabled channels** (too many fetch errors):")
            for dsub in disabled:
                dch = dsub.channel
                lines.append(
                    f"  @{dch.username} -- {dch.fetch_error_count} errors. "
                    "Use `/unsubscribe` then `/subscribe` to re-enable."
                )

        await self._formatter.safe_reply(ctx.message, "\n".join(lines))

    _subscribe_atomic = staticmethod(async_subscribe_channel_atomic)

    async def handle_subscribe(self, ctx: CommandExecutionContext) -> None:
        """Handle /subscribe @channel_name command."""
        if not self._cfg.digest.enabled:
            await self._formatter.safe_reply(ctx.message, "Channel digest is not enabled.")
            return

        parts = ctx.text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await self._formatter.safe_reply(
                ctx.message,
                "Usage: `/subscribe @channel_name`",
            )
            return

        username, error = parse_channel_input(parts[1])
        if error:
            await self._formatter.safe_reply(ctx.message, error)
            return

        status = await self._subscribe_atomic(ctx.uid, username, db=self._db)

        if status == "already_subscribed":
            await self._formatter.safe_reply(ctx.message, f"Already subscribed to @{username}.")
        elif status == "reactivated":
            await self._formatter.safe_reply(
                ctx.message, f"Reactivated subscription to @{username}."
            )
        else:
            await self._formatter.safe_reply(
                ctx.message,
                f"Subscribed to @{username}.\n\n"
                "Use `/digest` to generate a digest now, or wait for the daily delivery.",
            )
            logger.info(
                "digest_subscribed",
                extra={"uid": ctx.uid, "channel": username, "cid": ctx.correlation_id},
            )

    _unsubscribe_atomic = staticmethod(async_unsubscribe_channel_atomic)

    async def handle_unsubscribe(self, ctx: CommandExecutionContext) -> None:
        """Handle /unsubscribe @channel_name command."""
        if not self._cfg.digest.enabled:
            await self._formatter.safe_reply(ctx.message, "Channel digest is not enabled.")
            return

        parts = ctx.text.strip().split(maxsplit=1)
        if len(parts) < 2:
            await self._formatter.safe_reply(
                ctx.message,
                "Usage: `/unsubscribe @channel_name`",
            )
            return

        username, error = parse_channel_input(parts[1])
        if error:
            await self._formatter.safe_reply(ctx.message, error)
            return

        status = await self._unsubscribe_atomic(ctx.uid, username, db=self._db)

        if status == "not_found":
            await self._formatter.safe_reply(ctx.message, f"Channel @{username} not found.")
        elif status == "not_subscribed":
            await self._formatter.safe_reply(ctx.message, f"Not subscribed to @{username}.")
        else:
            await self._formatter.safe_reply(ctx.message, f"Unsubscribed from @{username}.")
            logger.info(
                "digest_unsubscribed",
                extra={"uid": ctx.uid, "channel": username, "cid": ctx.correlation_id},
            )
