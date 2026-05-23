"""Social account management commands."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_handlers.decorators import combined_handler
from app.application.ports.social_connections import SUPPORTED_SOCIAL_PROVIDERS
from app.application.services.social_auth_service import SocialAuthError
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )

logger = get_logger(__name__)

_PROVIDER_LABELS: dict[str, str] = {
    "x": "X",
    "instagram": "Instagram",
    "threads": "Threads",
}


class SocialHandler:
    """Implementation of Telegram social account commands."""

    def __init__(self, social_auth_service: Any) -> None:
        self._social_auth_service = social_auth_service

    @combined_handler("command_social", "social_status")
    async def handle_social(self, ctx: CommandExecutionContext) -> None:
        """Show connected social account status without token material."""
        try:
            result = await self._social_auth_service.list_connections(ctx.uid)
        except SocialAuthError as exc:
            await self._reply_social_error(ctx, exc, event_name="command_social_failed")
            return

        lines = ["Social accounts:"]
        for connection in result.connections:
            lines.append(_format_connection_status(connection))
        lines.append("")
        lines.append("Connect: /connect_x, /connect_threads, /connect_instagram")
        lines.append("Disconnect: /disconnect_social <provider>")
        await ctx.response_formatter.safe_reply(ctx.message, "\n".join(lines))

    @combined_handler("command_connect_x", "social_connect_x")
    async def handle_connect_x(self, ctx: CommandExecutionContext) -> None:
        """Return an X OAuth connect URL."""
        await self._handle_connect(ctx, provider="x")

    @combined_handler("command_connect_threads", "social_connect_threads")
    async def handle_connect_threads(self, ctx: CommandExecutionContext) -> None:
        """Return a Threads OAuth connect URL."""
        await self._handle_connect(ctx, provider="threads")

    @combined_handler("command_connect_instagram", "social_connect_instagram")
    async def handle_connect_instagram(self, ctx: CommandExecutionContext) -> None:
        """Return an Instagram OAuth connect URL."""
        await self._handle_connect(ctx, provider="instagram")

    @combined_handler("command_disconnect_social", "social_disconnect")
    async def handle_disconnect_social(self, ctx: CommandExecutionContext) -> None:
        """Disconnect a provider and remove local token state."""
        provider = _parse_disconnect_provider(ctx.text)
        if provider is None:
            await ctx.response_formatter.safe_reply(
                ctx.message,
                "Usage: /disconnect_social <x|instagram|threads>",
            )
            return

        try:
            result = await self._social_auth_service.disconnect(
                user_id=ctx.uid,
                provider=provider,
            )
        except SocialAuthError as exc:
            await self._reply_social_error(ctx, exc, event_name="command_disconnect_social_failed")
            return

        label = _provider_label(result.provider)
        if result.disconnected:
            text = f"{label} disconnected. Local token state was removed."
        else:
            text = f"{label} was not connected."
        await ctx.response_formatter.safe_reply(ctx.message, text)

    async def _handle_connect(self, ctx: CommandExecutionContext, *, provider: str) -> None:
        try:
            result = await self._social_auth_service.create_connect_url(
                user_id=ctx.uid,
                provider=provider,
                redirect_uri=None,
            )
        except SocialAuthError as exc:
            await self._reply_social_error(
                ctx, exc, event_name=f"command_connect_{provider}_failed"
            )
            return

        label = _provider_label(result.provider)
        text = "\n".join(
            [
                f"Connect {label}:",
                result.connect_url,
                "",
                f"This link expires at {result.expires_at}.",
            ]
        )
        await ctx.response_formatter.safe_reply(ctx.message, text, disable_web_page_preview=True)

    async def _reply_social_error(
        self,
        ctx: CommandExecutionContext,
        exc: SocialAuthError,
        *,
        event_name: str,
    ) -> None:
        logger.info(
            event_name,
            extra={
                "uid": ctx.uid,
                "cid": ctx.correlation_id,
                "reason_code": exc.code,
                "provider": exc.details.get("provider"),
            },
        )
        await ctx.response_formatter.send_error_notification(
            ctx.message,
            "social_auth_error",
            ctx.correlation_id,
            details=f"{exc.message} ({exc.code})",
        )


def _format_connection_status(connection: Any) -> str:
    label = _provider_label(connection.provider)
    status = str(connection.status or "disconnected")
    if not getattr(connection, "connected", False):
        return f"- {label}: {status}"

    parts = [f"- {label}: {status}"]
    username = getattr(connection, "provider_username", None)
    if username:
        parts.append(f"@{username}")
    scopes = getattr(connection, "token_scopes", None) or []
    if scopes:
        parts.append(f"scopes: {', '.join(scopes)}")
    expires_at = getattr(connection, "access_token_expires_at", None)
    if expires_at:
        parts.append(f"expires: {expires_at}")
    last_used_at = getattr(connection, "last_used_at", None)
    if last_used_at:
        parts.append(f"last used: {last_used_at}")
    return " | ".join(parts)


def _parse_disconnect_provider(text: str) -> str | None:
    tokens = text.split()
    if len(tokens) < 2:
        return None
    provider = tokens[1].strip().lower()
    if provider.startswith("@"):
        return None
    if provider not in SUPPORTED_SOCIAL_PROVIDERS:
        return None
    return provider


def _provider_label(provider: str) -> str:
    return _PROVIDER_LABELS.get(provider, provider.title())
