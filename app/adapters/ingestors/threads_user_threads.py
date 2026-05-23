"""Authenticated Threads user-threads source ingestor."""

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

_THREADS_FIELDS = (
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


@dataclass(slots=True, frozen=True)
class ThreadsUserThreadsIngestionConfig:
    enabled: bool = False
    user_id: int = 0
    limit: int = 30
    graph_base_url: str = "https://graph.threads.net/v1.0"


class ThreadsUserThreadsIngester:
    """Poll `/me/threads` for one authenticated Threads connection."""

    def __init__(
        self,
        *,
        config: ThreadsUserThreadsIngestionConfig,
        social_connections: SocialConnectionRepositoryPort,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._social_connections = social_connections
        self._client = client
        self.name = f"threads_user_threads:{config.user_id}"

    def is_enabled(self) -> bool:
        return self.config.enabled

    def source_identity(self) -> IngestedSource:
        return IngestedSource(
            kind="threads_user_threads",
            external_id=f"threads:telegram:{self.config.user_id}:user_threads",
            title="Threads posts",
            metadata={"provider": "threads", "endpoint": "/me/threads"},
        )

    async def fetch(self) -> SourceFetchResult:
        connection = await self._social_connections.get_by_user_and_provider(
            self.config.user_id,
            "threads",
        )
        skip = _connection_skip_reason(connection)
        if skip is not None:
            return SourceFetchResult(
                source=self.source_identity(),
                not_modified=True,
                metadata={"connection_status": skip},
            )
        assert connection is not None
        if "threads_basic" not in set(connection.token_scopes or []):
            raise AuthSourceError("Threads connection is missing threads_basic scope")
        if connection.encrypted_access_token is None:
            raise AuthSourceError("Threads connection is missing access token")

        payload = await self._fetch_threads(
            access_token=decrypt_secret(connection.encrypted_access_token),
        )
        return SourceFetchResult(
            source=IngestedSource(
                kind="threads_user_threads",
                external_id=f"threads:telegram:{self.config.user_id}:user_threads",
                url=f"https://www.threads.net/@{connection.provider_username}"
                if connection.provider_username
                else None,
                title=f"Threads @{connection.provider_username}"
                if connection.provider_username
                else "Threads posts",
                metadata={
                    "provider": "threads",
                    "endpoint": "/me/threads",
                    "provider_user_id": connection.provider_user_id,
                    "provider_username": connection.provider_username,
                },
            ),
            items=_normalize_threads_items(payload),
        )

    async def _fetch_threads(self, *, access_token: str) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        close_client = self._client is None
        try:
            response = await client.get(
                f"{self.config.graph_base_url.rstrip('/')}/me/threads",
                params={
                    "fields": ",".join(_THREADS_FIELDS),
                    "limit": str(max(1, min(int(self.config.limit), 100))),
                    "access_token": access_token,
                },
            )
        finally:
            if close_client:
                await client.aclose()
        raise_for_social_response(response, provider="Threads")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransientSourceError("Threads API response was not JSON") from exc
        if not isinstance(payload, dict):
            raise TransientSourceError("Threads API response was not an object")
        return payload


def _connection_skip_reason(connection: SocialConnectionRecord | None) -> str | None:
    if connection is None:
        return "missing"
    if connection.status != "active":
        return connection.status
    return None


def _normalize_threads_items(payload: dict[str, Any]) -> list[IngestedFeedItem]:
    data = payload.get("data") if isinstance(payload.get("data"), list) else []
    items: list[IngestedFeedItem] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        post_id = str(raw.get("id") or "").strip()
        if not post_id:
            continue
        permalink = _string_or_none(raw.get("permalink"))
        canonical_url = permalink
        if canonical_url is not None:
            try:
                canonical_url = normalize_url(canonical_url)
            except ValueError:
                pass
        text = _string_or_none(raw.get("text"))
        items.append(
            IngestedFeedItem(
                external_id=f"threads:{post_id}",
                canonical_url=canonical_url,
                title=text.replace("\n", " ")[:120] if text else None,
                content_text=text,
                author=_string_or_none(raw.get("username")),
                published_at=parse_datetime(raw.get("timestamp")),
                metadata={
                    "provider": "threads",
                    "threads_media_id": post_id,
                    "media_type": _string_or_none(raw.get("media_type")),
                    "shortcode": _string_or_none(raw.get("shortcode")),
                    "link_attachment_url": _string_or_none(raw.get("link_attachment_url")),
                    "is_quote_post": raw.get("is_quote_post")
                    if isinstance(raw.get("is_quote_post"), bool)
                    else None,
                },
            )
        )
    return items


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
