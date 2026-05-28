"""Instagram Graph API client for Instagram Login."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from app.adapters.social.meta.oauth import (
    InstagramOAuthConfig,
    InstagramOAuthError,
    build_instagram_authorization_url,
    exchange_instagram_authorization_code,
    refresh_instagram_access_token,
)
from app.application.services.social_auth_service import SocialAuthError

if TYPE_CHECKING:
    from app.application.dto.social_auth import OAuthTokenResult

INSTAGRAM_USER_FIELDS = (
    "id",
    "user_id",
    "username",
    "name",
    "account_type",
    "profile_picture_url",
    "followers_count",
    "follows_count",
    "media_count",
)
INSTAGRAM_MEDIA_FIELDS = (
    "id",
    "caption",
    "media_type",
    "media_url",
    "permalink",
    "thumbnail_url",
    "timestamp",
    "username",
    "children",
    "alt_text",
)


@dataclass(frozen=True, slots=True)
class InstagramMedia:
    """Normalized Instagram media object returned by the official API."""

    id: str
    caption: str | None = None
    media_type: str | None = None
    media_url: str | None = None
    permalink: str | None = None
    thumbnail_url: str | None = None
    timestamp: str | None = None
    username: str | None = None
    children: dict[str, Any] | None = None
    alt_text: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> InstagramMedia:
        return cls(
            id=str(payload.get("id") or ""),
            caption=_string_or_none(payload.get("caption")),
            media_type=_string_or_none(payload.get("media_type")),
            media_url=_string_or_none(payload.get("media_url")),
            permalink=_string_or_none(payload.get("permalink")),
            thumbnail_url=_string_or_none(payload.get("thumbnail_url")),
            timestamp=_string_or_none(payload.get("timestamp")),
            username=_string_or_none(payload.get("username")),
            children=_dict_or_none(payload.get("children")),
            alt_text=_string_or_none(payload.get("alt_text")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "caption": self.caption,
            "media_type": self.media_type,
            "media_url": self.media_url,
            "permalink": self.permalink,
            "thumbnail_url": self.thumbnail_url,
            "timestamp": self.timestamp,
            "username": self.username,
            "children": self.children,
            "alt_text": self.alt_text,
        }


class InstagramClient:
    """OAuth and read-only Instagram Graph API client.

    This client targets Instagram API with Instagram Login for professional accounts. It does not implement private-feed access, personal-account scraping, or username/password login.
    """

    def __init__(
        self,
        config: InstagramOAuthConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._owned_client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is not None:
            return self._http_client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=httpx.Timeout(self._config.timeout_sec))
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    def build_authorization_url(
        self,
        *,
        provider: str,
        state: str,
        code_challenge: str,
        redirect_uri: str,
        scopes: list[str],
    ) -> str:
        del provider, code_challenge
        return build_instagram_authorization_url(
            config=self._config,
            state=state,
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
        del provider, code_verifier, scopes, correlation_id
        try:
            token_result = await exchange_instagram_authorization_code(
                config=self._config,
                code=code,
                redirect_uri=redirect_uri,
                http_client=self._http_client,
            )
            me = await self.get_me(access_token=token_result.access_token)
        except InstagramOAuthError as exc:
            raise _to_social_auth_error(exc) from exc
        return token_result.__class__(
            access_token=token_result.access_token,
            refresh_token=token_result.refresh_token,
            scopes=token_result.scopes or self._config.default_scopes,
            access_token_expires_at=token_result.access_token_expires_at,
            refresh_token_expires_at=token_result.refresh_token_expires_at,
            provider_user_id=_string_or_none(me.get("user_id")) or token_result.provider_user_id,
            provider_username=_string_or_none(me.get("username")),
            metadata_json={
                **(token_result.metadata_json or {}),
                "instagram_account": _safe_me(me),
            },
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
            return await refresh_instagram_access_token(
                config=self._config,
                refresh_token=refresh_token,
                http_client=self._http_client,
            )
        except InstagramOAuthError as exc:
            raise _to_social_auth_error(exc, refresh=True) from exc

    async def refresh_token(self, *, refresh_token: str) -> OAuthTokenResult:
        """Refresh a long-lived Instagram token outside the provider-neutral OAuth port."""
        try:
            return await refresh_instagram_access_token(
                config=self._config,
                refresh_token=refresh_token,
                http_client=self._http_client,
            )
        except InstagramOAuthError as exc:
            raise _to_social_auth_error(exc, refresh=True) from exc

    async def get_me(self, *, access_token: str) -> dict[str, Any]:
        return await self._get_json(
            "me",
            access_token=access_token,
            params={"fields": ",".join(INSTAGRAM_USER_FIELDS)},
        )

    async def get_media_by_id(self, media_id: str, *, access_token: str) -> InstagramMedia:
        payload = await self.get_media_payload(media_id, access_token=access_token)
        return InstagramMedia.from_payload(payload)

    async def get_media_payload(self, media_id: str, *, access_token: str) -> dict[str, Any]:
        return await self._get_json(
            media_id,
            access_token=access_token,
            params={"fields": ",".join(INSTAGRAM_MEDIA_FIELDS)},
        )

    async def get_user_media_ids(
        self,
        ig_user_id: str,
        *,
        access_token: str,
        limit: int | None = None,
        before: str | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {}
        if limit is not None:
            params["limit"] = str(limit)
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        payload = await self._get_json(
            f"{ig_user_id}/media",
            access_token=access_token,
            params=params,
        )
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        return {
            "data": [
                {"id": str(item["id"])}
                for item in data
                if isinstance(item, dict) and item.get("id")
            ],
            "paging": payload.get("paging") if isinstance(payload.get("paging"), dict) else None,
        }

    async def _get_json(
        self,
        path: str,
        *,
        access_token: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        client = self._http()
        query = {**params, "access_token": access_token}
        url = f"{self._config.normalized_graph_base_url}/{path.lstrip('/')}"
        try:
            response = await client.get(url, params=query)
        except httpx.HTTPError as exc:
            raise InstagramOAuthError(
                "Instagram Graph request failed",
                code="INSTAGRAM_GRAPH_REQUEST_FAILED",
            ) from exc
        if response.status_code >= 400:
            raise InstagramOAuthError(
                "Instagram Graph request was rejected",
                code="INSTAGRAM_GRAPH_REQUEST_REJECTED",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise InstagramOAuthError(
                "Instagram Graph response was not JSON",
                code="INSTAGRAM_GRAPH_INVALID_JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise InstagramOAuthError(
                "Instagram Graph response was not an object",
                code="INSTAGRAM_GRAPH_INVALID_JSON",
            )
        return payload


def _to_social_auth_error(exc: InstagramOAuthError, *, refresh: bool = False) -> SocialAuthError:
    code = "INSTAGRAM_REFRESH_FAILED" if refresh else exc.code
    return SocialAuthError(
        exc.message,
        code=code,
        status_code=exc.status_code,
        details={"provider": "instagram"},
    )


def _safe_me(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in (
            "id",
            "user_id",
            "username",
            "name",
            "account_type",
            "profile_picture_url",
            "followers_count",
            "follows_count",
            "media_count",
        )
        if key in payload
    }


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def redact_instagram_url(url: str) -> str:
    """Return a token-redacted URL for tests/debugging without logging secrets."""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(key, "[REDACTED]" if key == "access_token" else value) for key, value in query]
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(redacted)))
