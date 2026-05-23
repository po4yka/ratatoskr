"""Authenticated X API v2 post lookup tier for Twitter/X extraction."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.application.ports.social_connections import SocialFetchAttemptCreate
from app.application.services.social_token_service import SocialAccessTokenResolver
from app.core.urls.twitter import extract_tweet_id
from app.core.url_utils import normalize_url

if TYPE_CHECKING:
    from app.adapters.social.x import XOAuthClient
    from app.application.ports.social_connections import (
        SocialConnectionRecord,
        SocialConnectionRepositoryPort,
    )

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class XApiExtractionResult:
    """Safe X API extraction result."""

    ok: bool
    content_text: str = ""
    content_source: str = "none"
    metadata: dict[str, Any] = field(default_factory=dict)


class XApiPostExtractor:
    """Fetch public X post content through a connected user's OAuth token."""

    def __init__(
        self,
        *,
        repository: SocialConnectionRepositoryPort,
        x_client: XOAuthClient,
        token_resolver: SocialAccessTokenResolver | None = None,
    ) -> None:
        self._repository = repository
        self._x_client = x_client
        self._token_resolver = token_resolver or SocialAccessTokenResolver(
            repository=repository,
            oauth_clients={"x": x_client},
        )

    async def extract(
        self,
        *,
        url_text: str,
        user_id: int | None,
        correlation_id: str | None,
        metadata: dict[str, Any],
    ) -> XApiExtractionResult:
        post_id = extract_tweet_id(url_text)
        logger.info(
            "social_content_fetch_started",
            extra={"cid": correlation_id, "provider": "x", "post_id": post_id},
        )
        if not post_id or user_id is None:
            return XApiExtractionResult(
                ok=False,
                metadata={"api_status": "skipped", "provider_resource_id": post_id},
            )

        token = await self._token_resolver.resolve(
            user_id=user_id,
            provider="x",
            required_scopes=("tweet.read", "users.read"),
            correlation_id=correlation_id,
        )
        if token.status in {"skipped", "no_connection"}:
            return XApiExtractionResult(
                ok=False,
                metadata={"api_status": token.status, "provider_resource_id": post_id},
            )
        connection = token.connection
        if not token.ok or connection is None or token.access_token is None:
            refresh_metadata = {
                "auth_strategy": {"selected_tier": "x_api"},
                "api_status": token.status,
                "provider_resource_id": post_id,
                "source_url": url_text,
                "normalized_url": _normalize_url_or_none(url_text),
                "correlation_id": correlation_id,
                **token.safe_metadata(),
            }
            if connection is not None:
                await self._record_attempt(
                    connection, user_id, "failed", refresh_metadata, token.status
                )
            logger.warning(
                "social_content_fetch_failed",
                extra={"cid": correlation_id, "provider": "x", "reason": token.status},
            )
            return XApiExtractionResult(ok=False, metadata=refresh_metadata)
        safe_metadata: dict[str, Any] = {
            "auth_strategy": {"selected_tier": "x_api"},
            "api_status": "started",
            "provider_resource_id": post_id,
            "source_url": url_text,
            "normalized_url": _normalize_url_or_none(url_text),
            "connection_id": connection.id,
            "correlation_id": correlation_id,
        }

        response = await self._x_client.get_post_by_id(
            post_id=post_id,
            access_token=token.access_token.get_secret_value(),
        )
        safe_metadata.update(_response_metadata(response.status_code, response.headers, post_id))
        if response.status_code == 401:
            await self._token_resolver.mark_needs_reauth(user_id=user_id, provider="x")
            await self._record_attempt(connection, user_id, "failed", safe_metadata, "unauthorized")
            logger.warning(
                "social_content_fetch_failed",
                extra={"cid": correlation_id, "provider": "x", "reason": "unauthorized"},
            )
            return XApiExtractionResult(ok=False, metadata=safe_metadata)
        if response.status_code in {403, 404, 429} or response.status_code >= 500:
            await self._record_attempt(
                connection,
                user_id,
                "failed",
                safe_metadata,
                _error_code_for_status(response.status_code),
            )
            logger.warning(
                "social_content_fetch_failed",
                extra={
                    "cid": correlation_id,
                    "provider": "x",
                    "status_code": response.status_code,
                },
            )
            return XApiExtractionResult(ok=False, metadata=safe_metadata)
        if response.status_code >= 400:
            await self._record_attempt(
                connection,
                user_id,
                "failed",
                safe_metadata,
                _error_code_for_status(response.status_code),
            )
            return XApiExtractionResult(ok=False, metadata=safe_metadata)

        try:
            payload = response.json()
        except ValueError:
            safe_metadata["api_status"] = "invalid_json"
            await self._record_attempt(connection, user_id, "failed", safe_metadata, "invalid_json")
            logger.warning(
                "social_content_fetch_failed",
                extra={"cid": correlation_id, "provider": "x", "reason": "invalid_json"},
            )
            return XApiExtractionResult(ok=False, metadata=safe_metadata)

        mapped = _map_post_payload(payload, post_id)
        if not mapped["content_text"]:
            safe_metadata["api_status"] = "empty"
            await self._record_attempt(connection, user_id, "failed", safe_metadata, "empty")
            logger.warning(
                "social_content_fetch_failed",
                extra={"cid": correlation_id, "provider": "x", "reason": "empty"},
            )
            return XApiExtractionResult(ok=False, metadata=safe_metadata)

        safe_metadata.update(mapped["metadata"])
        safe_metadata["api_status"] = "ok"
        safe_metadata["extraction_method"] = "x_api"
        safe_metadata["tier_outcomes"] = {**metadata.get("tier_outcomes", {}), "x_api": "success"}
        await self._record_attempt(connection, user_id, "succeeded", safe_metadata, None)
        logger.info(
            "social_content_fetch_succeeded",
            extra={"cid": correlation_id, "provider": "x", "post_id": post_id},
        )
        return XApiExtractionResult(
            ok=True,
            content_text=mapped["content_text"],
            content_source="x_api",
            metadata=safe_metadata,
        )

    async def _record_attempt(
        self,
        connection: SocialConnectionRecord,
        user_id: int,
        status: str,
        metadata: dict[str, Any],
        error_code: str | None,
    ) -> None:
        await self._repository.record_fetch_attempt(
            SocialFetchAttemptCreate(
                user_id=user_id,
                provider="x",
                connection_id=connection.id,
                attempt_type="post_lookup",
                status=status,
                error_code=error_code,
                error_message=error_code,
                source_url=_string(metadata.get("source_url")),
                normalized_url=_string(metadata.get("normalized_url")),
                provider_resource_id=_string(metadata.get("provider_resource_id")),
                http_status=_http_status(metadata.get("api_status")),
                auth_tier=_selected_tier(metadata),
                correlation_id=_string(metadata.get("correlation_id")),
                metadata_json=_safe_attempt_metadata(metadata),
            )
        )


def _response_metadata(status_code: int, headers: Any, post_id: str) -> dict[str, Any]:
    reset = headers.get("x-rate-limit-reset") if hasattr(headers, "get") else None
    metadata: dict[str, Any] = {
        "api_status": str(status_code),
        "provider_resource_id": post_id,
    }
    if reset:
        metadata["rate_limit"] = {"reset": reset}
    return metadata


def _map_post_payload(payload: dict[str, Any], post_id: str) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"content_text": "", "metadata": {"provider_resource_id": post_id}}
    includes = payload.get("includes") if isinstance(payload.get("includes"), dict) else {}
    users = _index_by_id(includes.get("users") if isinstance(includes, dict) else None)
    media = _index_by_key(includes.get("media") if isinstance(includes, dict) else None)
    author = users.get(str(data.get("author_id") or ""))
    media_items = _map_media(data, media)
    metrics = data.get("public_metrics") if isinstance(data.get("public_metrics"), dict) else {}
    urls = _extract_urls(data)

    author_name = _string(author.get("name")) if author else None
    author_username = _string(author.get("username")) if author else None
    text = _string(data.get("text")) or ""
    content_lines = []
    if author_name or author_username:
        handle = f"@{author_username}" if author_username else ""
        content_lines.append(" ".join(part for part in [author_name, handle] if part).strip())
    if data.get("created_at"):
        content_lines.append(f"Posted at: {data['created_at']}")
    content_lines.append(text)
    if urls:
        content_lines.append("Links: " + ", ".join(urls))
    if metrics:
        metric_text = ", ".join(f"{key}: {value}" for key, value in sorted(metrics.items()))
        content_lines.append(f"Metrics: {metric_text}")

    metadata = {
        "tweet_id": str(data.get("id") or post_id),
        "provider_resource_id": str(data.get("id") or post_id),
        "author": author_name,
        "author_handle": author_username,
        "created_at": _string(data.get("created_at")),
        "lang": _string(data.get("lang")),
        "public_metrics": metrics,
        "urls": urls,
        "tweet_media": media_items,
    }
    return {
        "content_text": "\n\n".join(line for line in content_lines if line),
        "metadata": metadata,
    }


def _map_media(
    data: dict[str, Any], media_by_key: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    attachments = data.get("attachments") if isinstance(data.get("attachments"), dict) else {}
    media_keys = attachments.get("media_keys") if isinstance(attachments, dict) else None
    if not isinstance(media_keys, list):
        return []
    result: list[dict[str, Any]] = []
    for index, key in enumerate(media_keys):
        item = media_by_key.get(str(key))
        if not item:
            continue
        url = _string(item.get("url")) or _string(item.get("preview_image_url"))
        if not url:
            continue
        result.append(
            {
                "url": url,
                "alt_text": _string(item.get("alt_text")),
                "media_index": index,
                "type": _string(item.get("type")),
                "tweet_id": data.get("id"),
            }
        )
    return result


def _extract_urls(data: dict[str, Any]) -> list[str]:
    entities = data.get("entities") if isinstance(data.get("entities"), dict) else {}
    raw_urls = entities.get("urls") if isinstance(entities, dict) else None
    if not isinstance(raw_urls, list):
        return []
    urls: list[str] = []
    for item in raw_urls:
        if not isinstance(item, dict):
            continue
        url = _string(item.get("expanded_url")) or _string(item.get("url"))
        if url and url not in urls:
            urls.append(url)
    return urls


def _index_by_id(items: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("id")): item for item in items if isinstance(item, dict) and item.get("id")
    }


def _index_by_key(items: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {
        str(item.get("media_key")): item
        for item in items
        if isinstance(item, dict) and item.get("media_key")
    }


def _safe_attempt_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "auth_strategy",
        "api_status",
        "connection_id",
        "correlation_id",
        "normalized_url",
        "provider_resource_id",
        "rate_limit",
        "source_url",
        "tweet_id",
    }
    return {key: value for key, value in metadata.items() if key in allowed}


def _selected_tier(metadata: dict[str, Any]) -> str | None:
    strategy = metadata.get("auth_strategy")
    if not isinstance(strategy, dict):
        return None
    value = strategy.get("selected_tier")
    return value if isinstance(value, str) and value else None


def _http_status(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _normalize_url_or_none(value: str) -> str | None:
    try:
        return normalize_url(value)
    except ValueError:
        return None


def _error_code_for_status(status_code: int) -> str:
    return {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        429: "rate_limited",
    }.get(status_code, "api_error")


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
