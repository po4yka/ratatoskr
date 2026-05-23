"""Authenticated X timeline source ingestor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from app.adapters.ingestors._social_common import (
    parse_datetime,
    raise_for_social_response,
    rate_limit_retry_at,
)
from app.application.ports.social_connections import SocialFetchAttemptCreate
from app.application.ports.source_ingestors import (
    AuthSourceError,
    IngestedFeedItem,
    IngestedSource,
    SourceFetchResult,
    TransientSourceError,
)
from app.core.url_utils import normalize_url

if TYPE_CHECKING:
    from app.application.ports.social_connections import (
        SocialConnectionRecord,
        SocialConnectionRepositoryPort,
    )
    from app.application.services.social_token_service import SocialAccessTokenResolver

_REQUIRED_X_READ_SCOPES = frozenset({"tweet.read", "users.read"})
_TWEET_FIELDS = (
    "author_id",
    "conversation_id",
    "created_at",
    "entities",
    "id",
    "lang",
    "possibly_sensitive",
    "public_metrics",
    "referenced_tweets",
    "text",
)
_USER_FIELDS = ("id", "name", "username", "verified")


@dataclass(slots=True, frozen=True)
class XTimelineIngestionConfig:
    enabled: bool = False
    user_id: int = 0
    timeline_mode: str = "user_posts"
    limit: int = 30
    api_base_url: str = "https://api.x.com/2"


class XTimelineIngester:
    """Poll one authenticated user's X user-posts or home timeline."""

    def __init__(
        self,
        *,
        config: XTimelineIngestionConfig,
        token_resolver: SocialAccessTokenResolver,
        social_connection_repository: SocialConnectionRepositoryPort | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._token_resolver = token_resolver
        self._social_connection_repository = social_connection_repository
        self._client = client
        self.name = f"x_timeline:{config.user_id}:{config.timeline_mode}"

    def is_enabled(self) -> bool:
        return self.config.enabled

    def source_identity(self) -> IngestedSource:
        return IngestedSource(
            kind="x_timeline",
            external_id=f"x:telegram:{self.config.user_id}:{self.config.timeline_mode}",
            url="https://x.com/home" if self.config.timeline_mode == "home_timeline" else None,
            title="X home timeline" if self.config.timeline_mode == "home_timeline" else "X posts",
            metadata={"provider": "x", "timeline_mode": self.config.timeline_mode},
        )

    async def fetch(self) -> SourceFetchResult:
        token = await self._token_resolver.resolve(
            user_id=self.config.user_id,
            provider="x",
            required_scopes=tuple(_REQUIRED_X_READ_SCOPES),
        )
        skip = _token_skip_reason(token.status)
        if skip is not None:
            return SourceFetchResult(
                source=self.source_identity(),
                not_modified=True,
                metadata={"connection_status": skip},
            )
        if token.status == "missing_scope":
            raise AuthSourceError("X connection is missing tweet.read/users.read scopes")
        if token.status == "missing_access_token":
            raise AuthSourceError("X connection is missing access token")
        if not token.ok or token.access_token is None or token.connection is None:
            raise AuthSourceError(f"X connection could not provide an access token: {token.status}")
        if not token.provider_user_id:
            raise AuthSourceError("X connection is missing provider user ID")

        payload = await self._fetch_timeline(
            provider_user_id=token.provider_user_id,
            access_token=token.access_token.get_secret_value(),
            connection=token.connection,
        )
        connection = token.connection
        items = _normalize_x_items(
            payload,
            provider_username=connection.provider_username,
            timeline_mode=self.config.timeline_mode,
        )
        return SourceFetchResult(
            source=IngestedSource(
                kind="x_timeline",
                external_id=f"x:telegram:{self.config.user_id}:{self.config.timeline_mode}",
                url="https://x.com/home"
                if self.config.timeline_mode == "home_timeline"
                else f"https://x.com/{connection.provider_username}"
                if connection.provider_username
                else None,
                title=f"X @{connection.provider_username}"
                if connection.provider_username
                else "X posts",
                metadata={
                    "provider": "x",
                    "timeline_mode": self.config.timeline_mode,
                    "provider_user_id": connection.provider_user_id,
                    "provider_username": connection.provider_username,
                },
            ),
            items=items,
        )

    async def _fetch_timeline(
        self,
        *,
        provider_user_id: str,
        access_token: str,
        connection: SocialConnectionRecord,
    ) -> dict[str, Any]:
        path = (
            f"users/{provider_user_id}/timelines/reverse_chronological"
            if self.config.timeline_mode == "home_timeline"
            else f"users/{provider_user_id}/tweets"
        )
        source_url = f"{self.config.api_base_url.rstrip('/')}/{path}"
        params = {
            "max_results": str(max(5, min(int(self.config.limit), 100))),
            "tweet.fields": ",".join(_TWEET_FIELDS),
            "expansions": "author_id",
            "user.fields": ",".join(_USER_FIELDS),
        }
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        close_client = self._client is None
        try:
            response = await client.get(
                source_url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        finally:
            if close_client:
                await client.aclose()
        if response.status_code >= 400:
            await self._record_attempt(
                connection=connection,
                status="failed",
                source_url=source_url,
                http_status=response.status_code,
                rate_limit_reset_at=rate_limit_retry_at(response.headers),
                error_code=_error_code_for_status(response.status_code),
            )
        else:
            await self._record_attempt(
                connection=connection,
                status="succeeded",
                source_url=source_url,
                http_status=response.status_code,
                rate_limit_reset_at=None,
                error_code=None,
            )
        raise_for_social_response(response, provider="X")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransientSourceError("X API response was not JSON") from exc
        if not isinstance(payload, dict):
            raise TransientSourceError("X API response was not an object")
        return payload

    async def _record_attempt(
        self,
        *,
        connection: SocialConnectionRecord,
        status: str,
        source_url: str,
        http_status: int,
        rate_limit_reset_at: Any,
        error_code: str | None,
    ) -> None:
        if self._social_connection_repository is None:
            return
        await self._social_connection_repository.record_fetch_attempt(
            SocialFetchAttemptCreate(
                user_id=self.config.user_id,
                provider="x",
                connection_id=connection.id,
                attempt_type=f"timeline:{self.config.timeline_mode}",
                status=status,
                error_code=error_code,
                error_message=error_code,
                source_url=source_url,
                normalized_url=source_url,
                provider_resource_id=connection.provider_user_id,
                http_status=http_status,
                auth_tier="x_timeline",
                rate_limit_reset_at=rate_limit_reset_at,
                metadata_json={
                    "api_status": str(http_status),
                    "auth_strategy": {"selected_tier": "x_timeline"},
                    "provider_resource_id": connection.provider_user_id,
                    "rate_limit": {"reset_at": rate_limit_reset_at.isoformat()}
                    if rate_limit_reset_at is not None
                    else {},
                },
            )
        )


def _token_skip_reason(status: str) -> str | None:
    if status == "no_connection":
        return "missing"
    if status in {"needs_reauth", "revoked", "disabled"}:
        return status
    return None


def _error_code_for_status(status_code: int) -> str:
    return {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        429: "rate_limited",
    }.get(status_code, "api_error")


def _normalize_x_items(
    payload: dict[str, Any],
    *,
    provider_username: str | None,
    timeline_mode: str,
) -> list[IngestedFeedItem]:
    data = payload.get("data") if isinstance(payload.get("data"), list) else []
    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    users = includes.get("users") if isinstance(includes.get("users"), list) else []
    users_by_id = {
        str(user.get("id")): user for user in users if isinstance(user, dict) and user.get("id")
    }
    items: list[IngestedFeedItem] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        tweet_id = str(raw.get("id") or "").strip()
        if not tweet_id:
            continue
        author = users_by_id.get(str(raw.get("author_id"))) or {}
        username = _string_or_none(author.get("username")) or provider_username
        canonical_url = f"https://x.com/{username}/status/{tweet_id}" if username else None
        if canonical_url is not None:
            try:
                canonical_url = normalize_url(canonical_url)
            except ValueError:
                pass
        metrics = raw.get("public_metrics") if isinstance(raw.get("public_metrics"), dict) else {}
        items.append(
            IngestedFeedItem(
                external_id=f"x:{tweet_id}",
                canonical_url=canonical_url,
                title=_title(raw.get("text")),
                content_text=_string_or_none(raw.get("text")),
                author=username,
                published_at=parse_datetime(raw.get("created_at")),
                engagement={
                    "score": _metric_score(metrics),
                    "comments": _int_or_none(metrics.get("reply_count")),
                    "forwards": _int_or_none(metrics.get("retweet_count")),
                },
                metadata={
                    "provider": "x",
                    "timeline_mode": timeline_mode,
                    "tweet_id": tweet_id,
                    "author_id": _string_or_none(raw.get("author_id")),
                    "like_count": _int_or_none(metrics.get("like_count")),
                    "quote_count": _int_or_none(metrics.get("quote_count")),
                },
            )
        )
    return items


def _title(value: Any) -> str | None:
    text = _string_or_none(value)
    if text is None:
        return None
    return text.replace("\n", " ")[:120]


def _metric_score(metrics: dict[str, Any]) -> float | None:
    values = [
        _int_or_none(metrics.get("like_count")),
        _int_or_none(metrics.get("retweet_count")),
        _int_or_none(metrics.get("reply_count")),
        _int_or_none(metrics.get("quote_count")),
    ]
    total = sum(value for value in values if value is not None)
    return float(total) if total else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
