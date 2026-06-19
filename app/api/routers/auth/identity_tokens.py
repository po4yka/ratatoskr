"""Shared token response helper for identity-provider auth routes."""

from __future__ import annotations

from typing import Any

from starlette.responses import Response  # noqa: TC002 - FastAPI route helper annotation

from app.api.models.responses import AuthTokensResponse, TokenPair, success_response
from app.api.routers.auth.cookies import set_refresh_cookie
from app.api.routers.auth.tokens import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    create_refresh_token,
    is_web_client,
)


async def issue_auth_tokens(
    *,
    user_id: int,
    username: str | None,
    client_id: str,
    response: Response,
) -> Any:
    """Issue the standard access/refresh token envelope for a login route."""
    access_token = create_access_token(user_id, username, client_id)
    refresh_token, session_id = await create_refresh_token(user_id, client_id)
    web = is_web_client(client_id)
    if web:
        set_refresh_cookie(response, refresh_token)
    tokens = TokenPair(
        access_token=access_token,
        refresh_token=None if web else refresh_token,
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        token_type="Bearer",
    )
    return success_response(AuthTokensResponse(tokens=tokens, session_id=session_id))
