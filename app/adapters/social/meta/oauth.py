"""Meta Threads OAuth helpers."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.application.dto.social_auth import OAuthTokenResult
from app.core.time_utils import UTC

THREADS_AUTHORIZATION_ENDPOINT = "https://threads.net/oauth/authorize"
THREADS_GRAPH_BASE_URL = "https://graph.threads.net/v1.0"
THREADS_OAUTH_ACCESS_TOKEN_ENDPOINT = "https://graph.threads.net/oauth/access_token"
THREADS_ACCESS_TOKEN_ENDPOINT = "https://graph.threads.net/access_token"
THREADS_REFRESH_ACCESS_TOKEN_ENDPOINT = "https://graph.threads.net/refresh_access_token"
THREADS_DEFAULT_SCOPES = ("threads_basic",)
INSTAGRAM_AUTHORIZATION_ENDPOINT = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_GRAPH_BASE_URL = "https://graph.instagram.com/v25.0"
INSTAGRAM_OAUTH_ACCESS_TOKEN_ENDPOINT = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_ACCESS_TOKEN_ENDPOINT = "https://graph.instagram.com/access_token"
INSTAGRAM_REFRESH_ACCESS_TOKEN_ENDPOINT = "https://graph.instagram.com/refresh_access_token"
INSTAGRAM_DEFAULT_SCOPES = ("instagram_business_basic",)


class ThreadsOAuthError(RuntimeError):
    """Token-safe Threads OAuth error."""

    def __init__(self, message: str, *, code: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class InstagramOAuthError(RuntimeError):
    """Token-safe Instagram OAuth error."""

    def __init__(self, message: str, *, code: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ThreadsOAuthConfig:
    """Runtime configuration for Threads OAuth and Graph API calls."""

    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None = None
    scopes: list[str] | None = None
    graph_base_url: str = THREADS_GRAPH_BASE_URL
    authorization_endpoint: str = THREADS_AUTHORIZATION_ENDPOINT
    oauth_access_token_endpoint: str = THREADS_OAUTH_ACCESS_TOKEN_ENDPOINT
    access_token_endpoint: str = THREADS_ACCESS_TOKEN_ENDPOINT
    refresh_access_token_endpoint: str = THREADS_REFRESH_ACCESS_TOKEN_ENDPOINT
    timeout_sec: float = 10.0

    @property
    def default_scopes(self) -> list[str]:
        return list(self.scopes or THREADS_DEFAULT_SCOPES)

    @property
    def normalized_graph_base_url(self) -> str:
        return self.graph_base_url.rstrip("/")


@dataclass(frozen=True, slots=True)
class InstagramOAuthConfig:
    """Runtime configuration for Instagram OAuth and Graph API calls."""

    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None = None
    scopes: list[str] | None = None
    graph_base_url: str = INSTAGRAM_GRAPH_BASE_URL
    authorization_endpoint: str = INSTAGRAM_AUTHORIZATION_ENDPOINT
    oauth_access_token_endpoint: str = INSTAGRAM_OAUTH_ACCESS_TOKEN_ENDPOINT
    access_token_endpoint: str = INSTAGRAM_ACCESS_TOKEN_ENDPOINT
    refresh_access_token_endpoint: str = INSTAGRAM_REFRESH_ACCESS_TOKEN_ENDPOINT
    timeout_sec: float = 10.0

    @property
    def default_scopes(self) -> list[str]:
        return list(self.scopes or INSTAGRAM_DEFAULT_SCOPES)

    @property
    def normalized_graph_base_url(self) -> str:
        return self.graph_base_url.rstrip("/")


def build_threads_authorization_url(
    *,
    config: ThreadsOAuthConfig,
    state: str,
    redirect_uri: str,
    scopes: list[str],
) -> str:
    client_id = _require_client_id(config)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
        "state": state,
    }
    return f"{config.authorization_endpoint}?{urllib.parse.urlencode(params)}"


def build_instagram_authorization_url(
    *,
    config: InstagramOAuthConfig,
    state: str,
    redirect_uri: str,
    scopes: list[str],
) -> str:
    client_id = _require_instagram_client_id(config)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(scopes),
        "response_type": "code",
        "state": state,
    }
    return f"{config.authorization_endpoint}?{urllib.parse.urlencode(params)}"


async def exchange_threads_authorization_code(
    *,
    config: ThreadsOAuthConfig,
    code: str,
    redirect_uri: str,
    http_client: httpx.AsyncClient | None = None,
) -> OAuthTokenResult:
    """Exchange an authorization code and immediately upgrade to a long-lived token."""
    client_id = _require_client_id(config)
    client_secret = _require_client_secret(config)
    short_lived = await _post_json(
        config=config,
        url=config.oauth_access_token_endpoint,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        http_client=http_client,
    )
    short_token = _require_access_token(short_lived)
    long_lived = await _get_json(
        config=config,
        url=config.access_token_endpoint,
        params={
            "grant_type": "th_exchange_token",
            "client_secret": client_secret,
            "access_token": short_token,
        },
        http_client=http_client,
    )
    return _token_payload_to_result(long_lived)


async def exchange_instagram_authorization_code(
    *,
    config: InstagramOAuthConfig,
    code: str,
    redirect_uri: str,
    http_client: httpx.AsyncClient | None = None,
) -> OAuthTokenResult:
    """Exchange an authorization code and immediately upgrade to a long-lived token."""
    client_id = _require_instagram_client_id(config)
    client_secret = _require_instagram_client_secret(config)
    short_lived = await _post_instagram_json(
        config=config,
        url=config.oauth_access_token_endpoint,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        http_client=http_client,
    )
    short_token = _require_instagram_access_token(short_lived)
    provider_user_id = _string_or_none(short_lived.get("user_id"))
    permissions = _parse_scopes(short_lived.get("permissions"))
    long_lived = await _get_instagram_json(
        config=config,
        url=config.access_token_endpoint,
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": client_secret,
            "access_token": short_token,
        },
        http_client=http_client,
    )
    return _instagram_token_payload_to_result(
        long_lived,
        provider_user_id=provider_user_id,
        scopes=permissions,
    )


async def refresh_threads_access_token(
    *,
    config: ThreadsOAuthConfig,
    refresh_token: str,
    http_client: httpx.AsyncClient | None = None,
) -> OAuthTokenResult:
    """Refresh a long-lived Threads token before it expires."""
    refreshed = await _get_json(
        config=config,
        url=config.refresh_access_token_endpoint,
        params={
            "grant_type": "th_refresh_token",
            "access_token": refresh_token,
        },
        http_client=http_client,
    )
    return _token_payload_to_result(refreshed)


async def refresh_instagram_access_token(
    *,
    config: InstagramOAuthConfig,
    refresh_token: str,
    http_client: httpx.AsyncClient | None = None,
) -> OAuthTokenResult:
    """Refresh a long-lived Instagram token before it expires."""
    refreshed = await _get_instagram_json(
        config=config,
        url=config.refresh_access_token_endpoint,
        params={
            "grant_type": "ig_refresh_token",
            "access_token": refresh_token,
        },
        http_client=http_client,
    )
    return _instagram_token_payload_to_result(refreshed)


async def _post_json(
    *,
    config: ThreadsOAuthConfig,
    url: str,
    data: dict[str, str],
    http_client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_sec))
    try:
        response = await client.post(url, data=data)
    except httpx.HTTPError as exc:
        raise ThreadsOAuthError(
            "Threads OAuth token request failed", code="THREADS_TOKEN_REQUEST_FAILED"
        ) from exc
    finally:
        if close_client:
            await client.aclose()
    return _parse_response(response)


async def _post_instagram_json(
    *,
    config: InstagramOAuthConfig,
    url: str,
    data: dict[str, str],
    http_client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_sec))
    try:
        response = await client.post(url, data=data)
    except httpx.HTTPError as exc:
        raise InstagramOAuthError(
            "Instagram OAuth token request failed",
            code="INSTAGRAM_TOKEN_REQUEST_FAILED",
        ) from exc
    finally:
        if close_client:
            await client.aclose()
    return _parse_instagram_response(response)


async def _get_json(
    *,
    config: ThreadsOAuthConfig,
    url: str,
    params: dict[str, str],
    http_client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_sec))
    try:
        response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise ThreadsOAuthError(
            "Threads OAuth token request failed", code="THREADS_TOKEN_REQUEST_FAILED"
        ) from exc
    finally:
        if close_client:
            await client.aclose()
    return _parse_response(response)


async def _get_instagram_json(
    *,
    config: InstagramOAuthConfig,
    url: str,
    params: dict[str, str],
    http_client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(config.timeout_sec))
    try:
        response = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise InstagramOAuthError(
            "Instagram OAuth token request failed",
            code="INSTAGRAM_TOKEN_REQUEST_FAILED",
        ) from exc
    finally:
        if close_client:
            await client.aclose()
    return _parse_instagram_response(response)


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise ThreadsOAuthError(
            "Threads OAuth token request was rejected",
            code="THREADS_TOKEN_REJECTED",
            status_code=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise ThreadsOAuthError(
            "Threads OAuth token response was not JSON", code="THREADS_TOKEN_INVALID_JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ThreadsOAuthError(
            "Threads OAuth token response was not an object", code="THREADS_TOKEN_INVALID_JSON"
        )
    return payload


def _parse_instagram_response(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= 400:
        raise InstagramOAuthError(
            "Instagram OAuth token request was rejected",
            code="INSTAGRAM_TOKEN_REJECTED",
            status_code=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise InstagramOAuthError(
            "Instagram OAuth token response was not JSON",
            code="INSTAGRAM_TOKEN_INVALID_JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise InstagramOAuthError(
            "Instagram OAuth token response was not an object",
            code="INSTAGRAM_TOKEN_INVALID_JSON",
        )
    return payload


def _token_payload_to_result(payload: dict[str, Any]) -> OAuthTokenResult:
    access_token = _require_access_token(payload)
    expires_in = _parse_expires_in(payload.get("expires_in"))
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None
    scopes = _parse_scopes(payload.get("scope"))
    return OAuthTokenResult(
        access_token=access_token,
        refresh_token=access_token,
        scopes=scopes,
        access_token_expires_at=expires_at.isoformat() if expires_at is not None else None,
        refresh_token_expires_at=expires_at.isoformat() if expires_at is not None else None,
        metadata_json={
            "token_type": payload.get("token_type"),
            "expires_in": expires_in,
            "threads_token_kind": "long_lived",
        },
    )


def _instagram_token_payload_to_result(
    payload: dict[str, Any],
    *,
    provider_user_id: str | None = None,
    scopes: list[str] | None = None,
) -> OAuthTokenResult:
    access_token = _require_instagram_access_token(payload)
    expires_in = _parse_expires_in(payload.get("expires_in"))
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in else None
    parsed_scopes = _parse_scopes(payload.get("scope"))
    return OAuthTokenResult(
        access_token=access_token,
        refresh_token=access_token,
        scopes=parsed_scopes or scopes,
        access_token_expires_at=expires_at.isoformat() if expires_at is not None else None,
        refresh_token_expires_at=expires_at.isoformat() if expires_at is not None else None,
        provider_user_id=provider_user_id,
        metadata_json={
            "token_type": payload.get("token_type"),
            "expires_in": expires_in,
            "instagram_token_kind": "long_lived",
        },
    )


def _parse_expires_in(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_scopes(value: Any) -> list[str] | None:
    if isinstance(value, str) and value.strip():
        return [scope.strip() for scope in value.replace(",", " ").split() if scope.strip()]
    if isinstance(value, list):
        scopes = [str(scope).strip() for scope in value if str(scope).strip()]
        return scopes or None
    return None


def _require_access_token(payload: dict[str, Any]) -> str:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ThreadsOAuthError(
            "Threads OAuth response did not include an access token",
            code="THREADS_ACCESS_TOKEN_MISSING",
        )
    return access_token


def _require_instagram_access_token(payload: dict[str, Any]) -> str:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise InstagramOAuthError(
            "Instagram OAuth response did not include an access token",
            code="INSTAGRAM_ACCESS_TOKEN_MISSING",
        )
    return access_token


def _require_client_id(config: ThreadsOAuthConfig) -> str:
    client_id = (config.client_id or "").strip()
    if not client_id:
        raise ThreadsOAuthError(
            "Threads client ID is not configured",
            code="THREADS_CLIENT_NOT_CONFIGURED",
            status_code=501,
        )
    return client_id


def _require_instagram_client_id(config: InstagramOAuthConfig) -> str:
    client_id = (config.client_id or "").strip()
    if not client_id:
        raise InstagramOAuthError(
            "Instagram client ID is not configured",
            code="INSTAGRAM_CLIENT_NOT_CONFIGURED",
            status_code=501,
        )
    return client_id


def _require_client_secret(config: ThreadsOAuthConfig) -> str:
    client_secret = (config.client_secret or "").strip()
    if not client_secret:
        raise ThreadsOAuthError(
            "Threads client secret is not configured",
            code="THREADS_CLIENT_SECRET_NOT_CONFIGURED",
            status_code=501,
        )
    return client_secret


def _require_instagram_client_secret(config: InstagramOAuthConfig) -> str:
    client_secret = (config.client_secret or "").strip()
    if not client_secret:
        raise InstagramOAuthError(
            "Instagram client secret is not configured",
            code="INSTAGRAM_CLIENT_SECRET_NOT_CONFIGURED",
            status_code=501,
        )
    return client_secret


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
