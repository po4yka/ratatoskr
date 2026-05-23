"""Threads Graph API client."""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from app.adapters.social.meta.oauth import (
    ThreadsOAuthConfig,
    ThreadsOAuthError,
    build_threads_authorization_url,
    exchange_threads_authorization_code,
    refresh_threads_access_token,
)
from app.application.services.social_auth_service import SocialAuthError

if TYPE_CHECKING:
    from app.application.dto.social_auth import OAuthTokenResult

THREADS_MEDIA_FIELDS = (
    "id",
    "media_product_type",
    "media_type",
    "media_url",
    "permalink",
    "owner",
    "username",
    "text",
    "timestamp",
    "shortcode",
    "thumbnail_url",
    "children",
    "is_quote_post",
    "quoted_post",
    "reposted_post",
    "alt_text",
    "link_attachment_url",
)


@dataclass(frozen=True, slots=True)
class ThreadsMedia:
    """Normalized Threads media object."""

    id: str
    media_product_type: str | None = None
    media_type: str | None = None
    media_url: str | None = None
    permalink: str | None = None
    owner: dict[str, Any] | None = None
    username: str | None = None
    text: str | None = None
    timestamp: str | None = None
    shortcode: str | None = None
    thumbnail_url: str | None = None
    children: list[dict[str, Any]] | None = None
    is_quote_post: bool | None = None
    quoted_post: dict[str, Any] | None = None
    reposted_post: dict[str, Any] | None = None
    alt_text: str | None = None
    link_attachment_url: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ThreadsMedia:
        return cls(
            id=str(payload.get("id") or ""),
            media_product_type=_string_or_none(payload.get("media_product_type")),
            media_type=_string_or_none(payload.get("media_type")),
            media_url=_string_or_none(payload.get("media_url")),
            permalink=_string_or_none(payload.get("permalink")),
            owner=_dict_or_none(payload.get("owner")),
            username=_string_or_none(payload.get("username")),
            text=_string_or_none(payload.get("text")),
            timestamp=_string_or_none(payload.get("timestamp")),
            shortcode=_string_or_none(payload.get("shortcode")),
            thumbnail_url=_string_or_none(payload.get("thumbnail_url")),
            children=_list_of_dicts_or_none(payload.get("children")),
            is_quote_post=payload.get("is_quote_post")
            if isinstance(payload.get("is_quote_post"), bool)
            else None,
            quoted_post=_dict_or_none(payload.get("quoted_post")),
            reposted_post=_dict_or_none(payload.get("reposted_post")),
            alt_text=_string_or_none(payload.get("alt_text")),
            link_attachment_url=_string_or_none(payload.get("link_attachment_url")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "media_product_type": self.media_product_type,
            "media_type": self.media_type,
            "media_url": self.media_url,
            "permalink": self.permalink,
            "owner": self.owner,
            "username": self.username,
            "text": self.text,
            "timestamp": self.timestamp,
            "shortcode": self.shortcode,
            "thumbnail_url": self.thumbnail_url,
            "children": self.children,
            "is_quote_post": self.is_quote_post,
            "quoted_post": self.quoted_post,
            "reposted_post": self.reposted_post,
            "alt_text": self.alt_text,
            "link_attachment_url": self.link_attachment_url,
        }


class ThreadsClient:
    """OAuth and read-only Threads Graph API client."""

    def __init__(
        self,
        config: ThreadsOAuthConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
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
        del provider, code_challenge
        return build_threads_authorization_url(
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
            token_result = await exchange_threads_authorization_code(
                config=self._config,
                code=code,
                redirect_uri=redirect_uri,
                http_client=self._http_client,
            )
            me = await self.get_me(access_token=token_result.access_token)
        except ThreadsOAuthError as exc:
            raise _to_social_auth_error(exc) from exc
        return token_result.__class__(
            access_token=token_result.access_token,
            refresh_token=token_result.refresh_token,
            scopes=token_result.scopes or self._config.default_scopes,
            access_token_expires_at=token_result.access_token_expires_at,
            refresh_token_expires_at=token_result.refresh_token_expires_at,
            provider_user_id=_string_or_none(me.get("id")),
            provider_username=_string_or_none(me.get("username")),
            metadata_json={**(token_result.metadata_json or {}), "threads_account": _safe_me(me)},
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
            return await refresh_threads_access_token(
                config=self._config,
                refresh_token=refresh_token,
                http_client=self._http_client,
            )
        except ThreadsOAuthError as exc:
            raise _to_social_auth_error(exc, refresh=True) from exc

    async def get_me(self, *, access_token: str) -> dict[str, Any]:
        return await self._get_json(
            "me",
            access_token=access_token,
            params={"fields": "id,username,name,threads_profile_picture_url,threads_biography"},
        )

    async def get_media(self, media_id: str, *, access_token: str) -> ThreadsMedia:
        payload = await self._get_json(
            media_id,
            access_token=access_token,
            params={"fields": ",".join(THREADS_MEDIA_FIELDS)},
        )
        return ThreadsMedia.from_payload(payload)

    async def get_user_threads(
        self,
        user_id: str = "me",
        *,
        access_token: str,
        limit: int | None = None,
        before: str | None = None,
        after: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"fields": ",".join(THREADS_MEDIA_FIELDS)}
        if limit is not None:
            params["limit"] = str(limit)
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        payload = await self._get_json(
            f"{user_id}/threads",
            access_token=access_token,
            params=params,
        )
        data = payload.get("data") if isinstance(payload.get("data"), list) else []
        return {
            "data": [
                ThreadsMedia.from_payload(item).to_dict()
                for item in data
                if isinstance(item, dict)
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
        close_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=httpx.Timeout(self._config.timeout_sec))
        query = {**params, "access_token": access_token}
        url = f"{self._config.normalized_graph_base_url}/{path.lstrip('/')}"
        try:
            response = await client.get(url, params=query)
        except httpx.HTTPError as exc:
            raise ThreadsOAuthError("Threads Graph request failed", code="THREADS_GRAPH_REQUEST_FAILED") from exc
        finally:
            if close_client:
                await client.aclose()
        if response.status_code >= 400:
            raise ThreadsOAuthError(
                "Threads Graph request was rejected",
                code="THREADS_GRAPH_REQUEST_REJECTED",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ThreadsOAuthError("Threads Graph response was not JSON", code="THREADS_GRAPH_INVALID_JSON") from exc
        if not isinstance(payload, dict):
            raise ThreadsOAuthError("Threads Graph response was not an object", code="THREADS_GRAPH_INVALID_JSON")
        return payload


def _to_social_auth_error(exc: ThreadsOAuthError, *, refresh: bool = False) -> SocialAuthError:
    code = "THREADS_REFRESH_FAILED" if refresh else exc.code
    return SocialAuthError(
        exc.message,
        code=code,
        status_code=exc.status_code,
        details={"provider": "threads"},
    )


def _safe_me(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in ("id", "username", "name", "threads_profile_picture_url", "threads_biography")
        if key in payload
    }


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _list_of_dicts_or_none(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    return [item for item in value if isinstance(item, dict)]


def redact_threads_url(url: str) -> str:
    """Return a token-redacted URL for tests/debugging without logging secrets."""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [
        (key, "[REDACTED]" if key == "access_token" else value)
        for key, value in query
    ]
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(redacted)))
