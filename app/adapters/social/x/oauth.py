"""X OAuth 2.0 Authorization Code with PKCE primitives."""

from __future__ import annotations

import base64
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.application.dto.social_auth import OAuthTokenResult
from app.core.time_utils import UTC

X_AUTHORIZATION_ENDPOINT = "https://x.com/i/oauth2/authorize"
X_TOKEN_ENDPOINT = "https://api.x.com/2/oauth2/token"
X_DEFAULT_SCOPES = ("tweet.read", "users.read", "offline.access")


class XOAuthError(RuntimeError):
    """X OAuth client error with token-safe details."""

    def __init__(self, message: str, *, code: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class XOAuthConfig:
    """Runtime configuration for X OAuth 2.0 with PKCE."""

    client_id: str | None
    client_secret: str | None = None
    redirect_uri: str | None = None
    scopes: list[str] | None = None
    api_base_url: str = "https://api.x.com/2"
    authorization_endpoint: str = X_AUTHORIZATION_ENDPOINT
    token_endpoint: str = X_TOKEN_ENDPOINT
    timeout_sec: float = 10.0

    @property
    def default_scopes(self) -> list[str]:
        return list(self.scopes or X_DEFAULT_SCOPES)

    @property
    def normalized_api_base_url(self) -> str:
        return self.api_base_url.rstrip("/")


@dataclass(frozen=True, slots=True)
class XOAuthTokenResponse:
    """Parsed X OAuth token response."""

    access_token: str
    refresh_token: str | None
    scopes: list[str] | None
    access_token_expires_at: datetime | None
    token_type: str | None
    raw_expires_in: int | None

    def to_oauth_result(
        self,
        *,
        provider_user_id: str | None = None,
        provider_username: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> OAuthTokenResult:
        return OAuthTokenResult(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            scopes=self.scopes,
            access_token_expires_at=_iso_or_none(self.access_token_expires_at),
            provider_user_id=provider_user_id,
            provider_username=provider_username,
            metadata_json={
                "token_type": self.token_type,
                "expires_in": self.raw_expires_in,
                **(metadata_json or {}),
            },
        )


def build_x_authorization_url(
    *,
    config: XOAuthConfig,
    state: str,
    code_challenge: str,
    redirect_uri: str,
    scopes: list[str],
) -> str:
    """Build an X authorization URL for Authorization Code with PKCE S256."""
    client_id = _require_client_id(config)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{config.authorization_endpoint}?{urllib.parse.urlencode(params)}"


async def exchange_x_authorization_code(
    *,
    config: XOAuthConfig,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    http_client: httpx.AsyncClient | None = None,
) -> XOAuthTokenResponse:
    """Exchange an X authorization code for an access token."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    return await _post_token(config=config, data=data, http_client=http_client)


async def refresh_x_access_token(
    *,
    config: XOAuthConfig,
    refresh_token: str,
    http_client: httpx.AsyncClient | None = None,
) -> XOAuthTokenResponse:
    """Use an X refresh token to obtain a new access token."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    return await _post_token(config=config, data=data, http_client=http_client)


async def _post_token(
    *,
    config: XOAuthConfig,
    data: dict[str, str],
    http_client: httpx.AsyncClient | None,
) -> XOAuthTokenResponse:
    client_id = _require_client_id(config)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    request_data = dict(data)
    if config.client_secret:
        headers["Authorization"] = _basic_auth_header(client_id, config.client_secret)
    else:
        request_data["client_id"] = client_id

    close_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_sec))
    try:
        response = await client.post(config.token_endpoint, data=request_data, headers=headers)
    except httpx.HTTPError as exc:
        raise XOAuthError(
            "X OAuth token request failed", code="X_OAUTH_TOKEN_REQUEST_FAILED"
        ) from exc
    finally:
        if close_client:
            await client.aclose()

    if response.status_code >= 400:
        raise XOAuthError(
            "X OAuth token request was rejected",
            code="X_OAUTH_TOKEN_REJECTED",
            status_code=response.status_code,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise XOAuthError(
            "X OAuth token response was not JSON", code="X_OAUTH_TOKEN_INVALID_JSON"
        ) from exc
    return parse_x_token_response(payload)


def parse_x_token_response(payload: dict[str, Any]) -> XOAuthTokenResponse:
    """Parse a token response without exposing token values in errors."""
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise XOAuthError(
            "X OAuth token response did not include an access token",
            code="X_OAUTH_ACCESS_TOKEN_MISSING",
        )
    refresh_token = payload.get("refresh_token")
    if refresh_token is not None and not isinstance(refresh_token, str):
        refresh_token = None
    scope = payload.get("scope")
    scopes = scope.split() if isinstance(scope, str) and scope.strip() else None
    expires_in = _parse_expires_in(payload.get("expires_in"))
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None
    token_type = payload.get("token_type")
    return XOAuthTokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        scopes=scopes,
        access_token_expires_at=expires_at,
        token_type=token_type if isinstance(token_type, str) else None,
        raw_expires_in=expires_in,
    )


def _parse_expires_in(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    return f"Basic {token}"


def _require_client_id(config: XOAuthConfig) -> str:
    client_id = (config.client_id or "").strip()
    if not client_id:
        raise XOAuthError(
            "X OAuth client ID is not configured",
            code="X_OAUTH_CLIENT_NOT_CONFIGURED",
            status_code=501,
        )
    return client_id


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
