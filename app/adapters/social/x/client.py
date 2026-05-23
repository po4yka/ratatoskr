"""Concrete X OAuth client used by social account connection workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from app.adapters.social.x.oauth import (
    XOAuthConfig,
    XOAuthError,
    build_x_authorization_url,
    exchange_x_authorization_code,
    refresh_x_access_token,
)
from app.application.services.social_auth_service import SocialAuthError

if TYPE_CHECKING:
    from app.application.dto.social_auth import OAuthTokenResult


class XOAuthClient:
    """X OAuth 2.0 Authorization Code with PKCE client."""

    def __init__(self, config: XOAuthConfig, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._http_client = http_client

    def build_authorization_url(
        self,
        *,
        provider: str,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: list[str],
    ) -> str:
        del provider
        return build_x_authorization_url(
            config=self._config,
            state=state,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )

    async def exchange_code(
        self,
        *,
        provider: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> OAuthTokenResult:
        del provider, scopes, correlation_id
        try:
            token_response = await exchange_x_authorization_code(
                config=self._config,
                code=code,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
                http_client=self._http_client,
            )
            user = await self._get_me(token_response.access_token)
        except XOAuthError as exc:
            raise _to_social_auth_error(exc) from exc
        return token_response.to_oauth_result(
            provider_user_id=user.get("id"),
            provider_username=user.get("username"),
        )

    async def refresh_access_token(
        self,
        *,
        provider: str,
        refresh_token: str,
        scopes: list[str],
        correlation_id: str | None,
    ) -> OAuthTokenResult:
        del provider, scopes, correlation_id
        try:
            token_response = await refresh_x_access_token(
                config=self._config,
                refresh_token=refresh_token,
                http_client=self._http_client,
            )
        except XOAuthError as exc:
            raise _to_social_auth_error(exc, refresh=True) from exc
        return token_response.to_oauth_result()

    async def _get_me(self, access_token: str) -> dict[str, str | None]:
        url = f"{self._config.normalized_api_base_url}/users/me"
        headers = {"Authorization": f"Bearer {access_token}"}
        close_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=httpx.Timeout(self._config.timeout_sec))
        try:
            response = await client.get(url, headers=headers)
        except httpx.HTTPError:
            return {"id": None, "username": None}
        finally:
            if close_client:
                await client.aclose()
        if response.status_code >= 400:
            return {"id": None, "username": None}
        try:
            payload = response.json()
        except ValueError:
            return {"id": None, "username": None}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return {"id": None, "username": None}
        return {
            "id": _string_or_none(data.get("id")),
            "username": _string_or_none(data.get("username")),
        }


def _to_social_auth_error(exc: XOAuthError, *, refresh: bool = False) -> SocialAuthError:
    code = "X_OAUTH_REFRESH_FAILED" if refresh else exc.code
    return SocialAuthError(
        exc.message,
        code=code,
        status_code=exc.status_code,
        details={"provider": "x"},
    )


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
