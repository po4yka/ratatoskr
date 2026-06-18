from __future__ import annotations

import hmac
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.api.exceptions import AuthorizationError, ValidationError
from app.api.routers.auth.tokens import decode_token, validate_client_id
from app.config import Config

logger = logging.getLogger("ratatoskr.mcp")

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class McpRequestIdentity:
    user_id: int
    client_id: str | None
    username: str | None
    auth_source: str


@dataclass(frozen=True)
class McpAuthenticationResult:
    identity: McpRequestIdentity | None
    status_code: int
    error: str | None = None


def _extract_bearer_token(value: str | None, *, allow_raw: bool = False) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.lower().startswith("bearer "):
        token = stripped[7:].strip()
        return token or None
    if allow_raw:
        return stripped
    return None


def _extract_auth_token(
    headers: Headers,
    *,
    forwarded_access_token_header: str,
    forwarded_secret_header: str,
    forwarding_secret: str | None,
) -> tuple[str | None, str | None, str | None]:
    direct_token = _extract_bearer_token(headers.get("authorization"))
    if direct_token:
        return direct_token, "authorization", None

    authorization = headers.get("authorization")
    if authorization:
        return None, None, "Authorization header must use the Bearer scheme"

    forwarded_token = headers.get(forwarded_access_token_header)
    if forwarded_token is None:
        return None, None, "Authentication required"

    if not forwarding_secret:
        logger.warning("mcp_forwarded_token_without_configured_secret")
        return None, None, "Forwarded token support is not configured"

    presented_secret = headers.get(forwarded_secret_header)
    if not presented_secret or not hmac.compare_digest(presented_secret, forwarding_secret):
        logger.warning("mcp_forwarded_token_secret_mismatch")
        return None, None, "Forwarded token credentials are invalid"

    token = _extract_bearer_token(forwarded_token, allow_raw=True)
    if token is None:
        return None, None, "Forwarded access token is invalid"
    return token, "forwarded_bearer", None


def authenticate_mcp_http_headers(
    headers: Headers | Mapping[str, str] | list[tuple[bytes, bytes]],
    *,
    forwarded_access_token_header: str,
    forwarded_secret_header: str,
    forwarding_secret: str | None,
) -> McpAuthenticationResult:
    normalized_headers = (
        headers
        if isinstance(headers, Headers)
        else Headers(headers=headers)
        if isinstance(headers, Mapping)
        else Headers(raw=headers)
    )
    token, auth_source, extraction_error = _extract_auth_token(
        normalized_headers,
        forwarded_access_token_header=forwarded_access_token_header,
        forwarded_secret_header=forwarded_secret_header,
        forwarding_secret=forwarding_secret,
    )
    if extraction_error is not None or token is None or auth_source is None:
        return McpAuthenticationResult(
            identity=None,
            status_code=HTTPStatus.UNAUTHORIZED,
            error=extraction_error or "Authentication required",
        )

    try:
        payload = decode_token(token, expected_type="access")
    except Exception:
        logger.info("mcp_http_auth_invalid_access_token", extra={"auth_source": auth_source})
        return McpAuthenticationResult(
            identity=None,
            status_code=HTTPStatus.UNAUTHORIZED,
            error="Invalid access token",
        )

    raw_user_id = payload.get("user_id")
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        return McpAuthenticationResult(
            identity=None,
            status_code=HTTPStatus.UNAUTHORIZED,
            error="Invalid access token payload",
        )

    if user_id <= 0:
        return McpAuthenticationResult(
            identity=None,
            status_code=HTTPStatus.UNAUTHORIZED,
            error="Invalid access token payload",
        )

    if not Config.is_user_allowed(user_id, fail_open_when_empty=False):
        return McpAuthenticationResult(
            identity=None,
            status_code=HTTPStatus.FORBIDDEN,
            error="User not authorized",
        )

    client_id_any = payload.get("client_id")
    client_id = str(client_id_any).strip() if client_id_any is not None else None
    try:
        validate_client_id(client_id)
    except (AuthorizationError, ValidationError) as exc:
        status_code = (
            HTTPStatus.FORBIDDEN if isinstance(exc, AuthorizationError) else HTTPStatus.UNAUTHORIZED
        )
        return McpAuthenticationResult(
            identity=None,
            status_code=status_code,
            error=str(exc),
        )

    username_any = payload.get("username")
    username = str(username_any).strip() if isinstance(username_any, str) else None
    return McpAuthenticationResult(
        identity=McpRequestIdentity(
            user_id=user_id,
            client_id=client_id,
            username=username,
            auth_source=auth_source,
        ),
        status_code=HTTPStatus.OK,
    )


class McpHttpAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        forwarded_access_token_header: str,
        forwarded_secret_header: str,
        forwarding_secret: str | None,
    ) -> None:
        self.app = app
        self.forwarded_access_token_header = forwarded_access_token_header
        self.forwarded_secret_header = forwarded_secret_header
        self.forwarding_secret = forwarding_secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        result = authenticate_mcp_http_headers(
            scope.get("headers", []),
            forwarded_access_token_header=self.forwarded_access_token_header,
            forwarded_secret_header=self.forwarded_secret_header,
            forwarding_secret=self.forwarding_secret,
        )
        if result.identity is None:
            response = JSONResponse(
                {
                    "error": "mcp_auth_failed",
                    "message": result.error or "Authentication required",
                },
                status_code=result.status_code,
            )
            await response(scope, receive, send)
            return

        state: dict[str, Any] = dict(scope.get("state") or {})
        state["mcp_identity"] = result.identity
        scope["state"] = state
        await self.app(scope, receive, send)
