"""Authenticated Threads user-threads source ingestor."""

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
        token_resolver: SocialAccessTokenResolver,
        social_connection_repository: SocialConnectionRepositoryPort | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._token_resolver = token_resolver
        self._social_connection_repository = social_connection_repository
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
        token = await self._token_resolver.resolve(
            user_id=self.config.user_id,
            provider="threads",
            required_scopes=("threads_basic",),
        )
        skip = _token_skip_reason(token.status)
        if skip is not None:
            return SourceFetchResult(
                source=self.source_identity(),
                not_modified=True,
                metadata={"connection_status": skip},
            )
        if token.status == "missing_scope":
            raise AuthSourceError("Threads connection is missing threads_basic scope")
        if token.status == "missing_access_token":
            raise AuthSourceError("Threads connection is missing access token")
        if not token.ok or token.access_token is None or token.connection is None:
            raise AuthSourceError(
                f"Threads connection could not provide an access token: {token.status}"
            )

        connection = token.connection
        payload = await self._fetch_threads(
            access_token=token.access_token.get_secret_value(),
            connection=connection,
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

    async def _fetch_threads(
        self,
        *,
        access_token: str,
        connection: SocialConnectionRecord,
    ) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        close_client = self._client is None
        source_url = f"{self.config.graph_base_url.rstrip('/')}/me/threads"
        try:
            response = await client.get(
                source_url,
                params={
                    "fields": ",".join(_THREADS_FIELDS),
                    "limit": str(max(1, min(int(self.config.limit), 100))),
                    "access_token": access_token,
                },
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
        raise_for_social_response(response, provider="Threads")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransientSourceError("Threads API response was not JSON") from exc
        if not isinstance(payload, dict):
            raise TransientSourceError("Threads API response was not an object")
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
                provider="threads",
                connection_id=connection.id,
                attempt_type="user_threads",
                status=status,
                error_code=error_code,
                error_message=error_code,
                source_url=source_url,
                normalized_url=source_url,
                provider_resource_id=connection.provider_user_id,
                http_status=http_status,
                auth_tier="threads_user_threads",
                rate_limit_reset_at=rate_limit_reset_at,
                metadata_json={
                    "api_status": str(http_status),
                    "auth_strategy": {"selected_tier": "threads_user_threads"},
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
