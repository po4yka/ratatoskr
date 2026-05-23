from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.telegram.command_handlers.execution_context import CommandExecutionContext
from app.adapters.telegram.command_handlers.social_handler import SocialHandler
from app.application.dto.social_auth import (
    SocialConnectUrlDTO,
    SocialConnectionDTO,
    SocialConnectionListDTO,
    SocialDisconnectDTO,
)


@dataclass
class _CreateConnectUrlCall:
    user_id: int
    provider: str
    redirect_uri: str | None


class _FakeSocialAuthService:
    def __init__(self) -> None:
        self.create_connect_url_calls: list[_CreateConnectUrlCall] = []
        self.disconnect_calls: list[tuple[int, str]] = []
        self.connections = SocialConnectionListDTO(
            connections=[
                SocialConnectionDTO(
                    provider="x",
                    connected=True,
                    auth_type="oauth2",
                    provider_user_id="x-user-id",
                    provider_username="reader",
                    token_scopes=["tweet.read", "offline.access"],
                    access_token_expires_at="2026-05-23T12:00:00+00:00",
                    refresh_token_expires_at=None,
                    last_used_at="2026-05-23T10:00:00+00:00",
                    status="active",
                    connected_at="2026-05-22T10:00:00+00:00",
                    updated_at="2026-05-23T10:00:00+00:00",
                    metadata_json={
                        "encrypted_access_token": "raw-token",
                        "callback_url": "https://example.com/callback?code=raw-code",
                    },
                ),
                SocialConnectionDTO(
                    provider="threads",
                    connected=False,
                    auth_type=None,
                    provider_user_id=None,
                    provider_username=None,
                    token_scopes=None,
                    access_token_expires_at=None,
                    refresh_token_expires_at=None,
                    last_used_at=None,
                    status="disconnected",
                    connected_at=None,
                    updated_at=None,
                    metadata_json=None,
                ),
            ]
        )

    async def list_connections(self, user_id: int) -> SocialConnectionListDTO:
        assert user_id == 1001
        return self.connections

    async def create_connect_url(
        self,
        *,
        user_id: int,
        provider: str,
        redirect_uri: str | None,
    ) -> SocialConnectUrlDTO:
        self.create_connect_url_calls.append(
            _CreateConnectUrlCall(user_id=user_id, provider=provider, redirect_uri=redirect_uri)
        )
        return SocialConnectUrlDTO(
            provider=provider,
            connect_url=f"https://auth.example.com/{provider}?state=safe-state",
            state="safe-state",
            scopes=["read.scope"],
            redirect_uri="https://app.example.com/social/callback",
            expires_at="2026-05-23T12:00:00+00:00",
        )

    async def disconnect(self, *, user_id: int, provider: str) -> SocialDisconnectDTO:
        self.disconnect_calls.append((user_id, provider))
        return SocialDisconnectDTO(provider=provider, disconnected=True)


def _ctx(text: str) -> CommandExecutionContext:
    formatter = MagicMock()
    formatter.safe_reply = AsyncMock()
    formatter.send_error_notification = AsyncMock()
    return CommandExecutionContext(
        message=MagicMock(),
        text=text,
        uid=1001,
        chat_id=2002,
        correlation_id="cid-social-command",
        interaction_id=3003,
        start_time=1.0,
        user_repo=MagicMock(),
        response_formatter=formatter,
        audit_func=MagicMock(),
    )


def _response_formatter(ctx: CommandExecutionContext) -> Any:
    return ctx.response_formatter


@pytest.mark.asyncio
async def test_social_lists_provider_status_without_tokens() -> None:
    service = _FakeSocialAuthService()
    handler = SocialHandler(service)
    ctx = _ctx("/social")

    await handler.handle_social(ctx)

    formatter = _response_formatter(ctx)
    formatter.safe_reply.assert_awaited_once()
    text = formatter.safe_reply.await_args.args[1]
    assert "X: active" in text
    assert "@reader" in text
    assert "Threads: disconnected" in text
    assert "raw-token" not in text
    assert "raw-code" not in text
    assert "encrypted_access_token" not in text
    assert "callback_url" not in text


@pytest.mark.asyncio
async def test_connect_x_returns_connect_url_from_service() -> None:
    service = _FakeSocialAuthService()
    handler = SocialHandler(service)
    ctx = _ctx("/connect_x")

    await handler.handle_connect_x(ctx)

    assert service.create_connect_url_calls == [
        _CreateConnectUrlCall(user_id=1001, provider="x", redirect_uri=None)
    ]
    formatter = _response_formatter(ctx)
    formatter.safe_reply.assert_awaited_once()
    reply_args = formatter.safe_reply.await_args
    assert "https://auth.example.com/x?state=safe-state" in reply_args.args[1]
    assert reply_args.kwargs["disable_web_page_preview"] is True


@pytest.mark.asyncio
async def test_connect_threads_and_instagram_use_provider_specific_service_calls() -> None:
    service = _FakeSocialAuthService()
    handler = SocialHandler(service)

    await handler.handle_connect_threads(_ctx("/connect_threads"))
    await handler.handle_connect_instagram(_ctx("/connect_instagram"))

    assert service.create_connect_url_calls == [
        _CreateConnectUrlCall(user_id=1001, provider="threads", redirect_uri=None),
        _CreateConnectUrlCall(user_id=1001, provider="instagram", redirect_uri=None),
    ]


@pytest.mark.asyncio
async def test_disconnect_social_removes_provider_connection() -> None:
    service = _FakeSocialAuthService()
    handler = SocialHandler(service)
    ctx = _ctx("/disconnect_social instagram")

    await handler.handle_disconnect_social(ctx)

    assert service.disconnect_calls == [(1001, "instagram")]
    formatter = _response_formatter(ctx)
    formatter.safe_reply.assert_awaited_once()
    assert "Instagram disconnected" in formatter.safe_reply.await_args.args[1]
    assert "token state was removed" in formatter.safe_reply.await_args.args[1]


@pytest.mark.asyncio
async def test_disconnect_social_requires_supported_provider() -> None:
    service = _FakeSocialAuthService()
    handler = SocialHandler(service)
    ctx = _ctx("/disconnect_social mastodon")

    await handler.handle_disconnect_social(ctx)

    assert service.disconnect_calls == []
    _response_formatter(ctx).safe_reply.assert_awaited_once_with(
        ctx.message,
        "Usage: /disconnect_social <x|instagram|threads>",
    )
