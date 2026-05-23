"""Authenticated X timeline source ingestor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from app.adapters.ingestors._social_common import parse_datetime, raise_for_social_response
from app.application.ports.source_ingestors import (
    AuthSourceError,
    IngestedFeedItem,
    IngestedSource,
    SourceFetchResult,
    TransientSourceError,
)
from app.core.url_utils import normalize_url
from app.security.secret_crypto import decrypt_secret

if TYPE_CHECKING:
    from app.application.ports.social_connections import (
        SocialConnectionRecord,
        SocialConnectionRepositoryPort,
    )

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
        social_connections: SocialConnectionRepositoryPort,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._social_connections = social_connections
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
        connection = await self._social_connections.get_by_user_and_provider(
            self.config.user_id,
            "x",
        )
        skip = _connection_skip_reason(connection)
        if skip is not None:
            return SourceFetchResult(
                source=self.source_identity(),
                not_modified=True,
                metadata={"connection_status": skip},
            )
        assert connection is not None
        if not _REQUIRED_X_READ_SCOPES.issubset(set(connection.token_scopes or [])):
            raise AuthSourceError("X connection is missing tweet.read/users.read scopes")
        if not connection.provider_user_id:
            raise AuthSourceError("X connection is missing provider user ID")
        if connection.encrypted_access_token is None:
            raise AuthSourceError("X connection is missing access token")

        payload = await self._fetch_timeline(
            provider_user_id=connection.provider_user_id,
            access_token=decrypt_secret(connection.encrypted_access_token),
        )
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

    async def _fetch_timeline(self, *, provider_user_id: str, access_token: str) -> dict[str, Any]:
        path = (
            f"users/{provider_user_id}/timelines/reverse_chronological"
            if self.config.timeline_mode == "home_timeline"
            else f"users/{provider_user_id}/tweets"
        )
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
                f"{self.config.api_base_url.rstrip('/')}/{path}",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        finally:
            if close_client:
                await client.aclose()
        raise_for_social_response(response, provider="X")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransientSourceError("X API response was not JSON") from exc
        if not isinstance(payload, dict):
            raise TransientSourceError("X API response was not an object")
        return payload


def _connection_skip_reason(connection: SocialConnectionRecord | None) -> str | None:
    if connection is None:
        return "missing"
    if connection.status != "active":
        return connection.status
    return None


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
