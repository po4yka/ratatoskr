"""Authenticated Threads API extraction tier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.application.dto.aggregation import (
    ExtractedTextKind,
    NormalizedSourceDocument,
    SourceMediaAsset,
    SourceMediaKind,
    SourceProvenance,
    SourceTextBlock,
)
from app.application.ports.social_connections import SocialFetchAttemptCreate
from app.application.services.social_token_service import SocialAccessTokenResolver
from app.core.lang import detect_language
from app.core.urls.meta import extract_threads_post_id
from app.domain.models.source import SourceItem, SourceKind

if TYPE_CHECKING:
    import httpx

    from app.adapters.social.meta import ThreadsClient, ThreadsMedia
    from app.application.ports.social_connections import (
        SocialConnectionRecord,
        SocialConnectionRepositoryPort,
    )


@dataclass(frozen=True, slots=True)
class ThreadsApiExtractionResult:
    ok: bool
    content_text: str = ""
    content_source: str = "none"
    title: str | None = None
    images: list[str] | None = None
    metadata: dict[str, Any] | None = None
    source_item: SourceItem | None = None
    normalized_document: NormalizedSourceDocument | None = None
    detected_lang: str | None = None


class ThreadsApiExtractor:
    """Use connected Threads credentials to retrieve a Threads media object."""

    def __init__(
        self,
        *,
        repository: SocialConnectionRepositoryPort,
        threads_client: ThreadsClient,
        token_resolver: SocialAccessTokenResolver | None = None,
    ) -> None:
        self._repository = repository
        self._threads_client = threads_client
        self._token_resolver = token_resolver or SocialAccessTokenResolver(
            repository=repository,
            oauth_clients={"threads": threads_client},
        )

    async def extract(
        self,
        *,
        url: str,
        user_id: int | None,
        request_id: int | None,
        dedupe_hash: str,
    ) -> ThreadsApiExtractionResult:
        media_id = extract_threads_post_id(url)
        base_metadata: dict[str, Any] = {
            "source": "meta",
            "platform": "threads",
            "platform_surface": SourceKind.THREADS_POST.value,
            "provider_resource_id": media_id,
            "api_status": "skipped",
            "auth_strategy": {
                "authenticated_supported": True,
                "selected_tier": "meta_scraper_fallback",
            },
        }
        if not media_id or user_id is None:
            return ThreadsApiExtractionResult(ok=False, metadata=base_metadata)

        token = await self._token_resolver.resolve(
            user_id=user_id,
            provider="threads",
            required_scopes=("threads_basic",),
        )
        if token.status in {"skipped", "no_connection"}:
            base_metadata["api_status"] = token.status
            return ThreadsApiExtractionResult(ok=False, metadata=base_metadata)

        connection = token.connection
        if not token.ok or connection is None or token.access_token is None:
            base_metadata.update(token.safe_metadata())
            if connection is not None:
                await self._record_attempt(
                    connection, user_id, "failed", base_metadata, token.status
                )
            return ThreadsApiExtractionResult(ok=False, metadata=base_metadata)

        response = await self._threads_client.get_media_response(
            media_id,
            access_token=token.access_token.get_secret_value(),
        )
        metadata = {
            **base_metadata,
            **_response_metadata(response, media_id),
            "connection_id": connection.id,
        }
        if response.status_code == 401:
            await self._token_resolver.mark_needs_reauth(user_id=user_id, provider="threads")
            await self._record_attempt(connection, user_id, "failed", metadata, "unauthorized")
            return ThreadsApiExtractionResult(ok=False, metadata=metadata)
        if response.status_code in {403, 404, 429} or response.status_code >= 500:
            await self._record_attempt(
                connection,
                user_id,
                "failed",
                metadata,
                _error_code_for_status(response.status_code),
            )
            return ThreadsApiExtractionResult(ok=False, metadata=metadata)
        if response.status_code >= 400:
            await self._record_attempt(connection, user_id, "failed", metadata, "api_error")
            return ThreadsApiExtractionResult(ok=False, metadata=metadata)

        try:
            payload = response.json()
        except ValueError:
            metadata["api_status"] = "invalid_json"
            await self._record_attempt(connection, user_id, "failed", metadata, "invalid_json")
            return ThreadsApiExtractionResult(ok=False, metadata=metadata)
        if not isinstance(payload, dict):
            metadata["api_status"] = "invalid_json"
            await self._record_attempt(connection, user_id, "failed", metadata, "invalid_json")
            return ThreadsApiExtractionResult(ok=False, metadata=metadata)

        from app.adapters.social.meta import ThreadsMedia

        threads_media = ThreadsMedia.from_payload(payload)
        result = _build_result_from_media(
            media=threads_media,
            url=url,
            request_id=request_id,
            dedupe_hash=dedupe_hash,
            metadata={**metadata, "api_status": "ok"},
        )
        await self._record_attempt(
            connection,
            user_id,
            "succeeded",
            result.metadata or metadata,
            None,
        )
        return result

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
                provider="threads",
                connection_id=connection.id,
                attempt_type="media_retrieval",
                status=status,
                error_code=error_code,
                error_message=error_code,
                metadata_json=_safe_attempt_metadata(metadata),
            )
        )


def _build_result_from_media(
    *,
    media: ThreadsMedia,
    url: str,
    request_id: int | None,
    dedupe_hash: str,
    metadata: dict[str, Any],
) -> ThreadsApiExtractionResult:
    text = media.text or ""
    quoted_text = _quoted_text(media)
    link_text = f"Link: {media.link_attachment_url}" if media.link_attachment_url else None
    content_text = "\n\n".join(part for part in (text, quoted_text, link_text) if part)
    media_assets = _media_assets(media)
    if not content_text and not media_assets:
        return ThreadsApiExtractionResult(ok=False, metadata={**metadata, "api_status": "empty"})

    source_item = SourceItem.create(
        kind=SourceKind.THREADS_POST,
        original_value=url,
        normalized_value=url,
        external_id=media.id,
        request_id=request_id,
        title_hint=None,
        metadata={
            "platform": "meta",
            "platform_surface": SourceKind.THREADS_POST.value,
            "dedupe_hash": dedupe_hash,
        },
    )
    text_blocks: list[SourceTextBlock] = []
    if text:
        text_blocks.append(SourceTextBlock(kind=ExtractedTextKind.BODY, text=text, position=0))
    if quoted_text:
        text_blocks.append(
            SourceTextBlock(
                kind=ExtractedTextKind.BODY,
                text=quoted_text,
                position=len(text_blocks),
                metadata={"role": "quoted_context"},
            )
        )
    if link_text:
        text_blocks.append(
            SourceTextBlock(
                kind=ExtractedTextKind.BODY,
                text=link_text,
                position=len(text_blocks),
                metadata={"role": "link_attachment"},
            )
        )
    detected_lang = detect_language(content_text)
    result_metadata = {
        **metadata,
        "auth_strategy": {
            "authenticated_supported": True,
            "selected_tier": "threads_api",
        },
        "threads_media": media.to_dict(),
        "request_id": request_id,
        "detected_lang": detected_lang,
    }
    normalized = NormalizedSourceDocument(
        source_item_id=source_item.stable_id,
        source_kind=source_item.kind,
        title=None,
        text=content_text,
        detected_language=detected_lang,
        text_blocks=text_blocks,
        media=media_assets,
        metadata=result_metadata,
        provenance=SourceProvenance(
            source_item_id=source_item.stable_id,
            source_kind=source_item.kind,
            original_value=source_item.original_value,
            normalized_value=source_item.normalized_value,
            external_id=source_item.external_id,
            request_id=request_id,
            extraction_source="threads_api",
            metadata={"dedupe_hash": dedupe_hash},
        ),
    )
    return ThreadsApiExtractionResult(
        ok=True,
        content_text=content_text,
        content_source="threads_api",
        images=[
            asset.url for asset in media_assets if asset.kind == SourceMediaKind.IMAGE and asset.url
        ],
        metadata=result_metadata,
        source_item=source_item,
        normalized_document=normalized,
        detected_lang=detected_lang,
    )


def _media_assets(media: ThreadsMedia) -> list[SourceMediaAsset]:
    assets: list[SourceMediaAsset] = []
    if media.media_url:
        assets.append(
            SourceMediaAsset(
                kind=_media_kind(media.media_type),
                url=media.media_url,
                position=len(assets),
                alt_text=media.alt_text,
                metadata={"source": "threads_api", "media_id": media.id},
            )
        )
    if media.thumbnail_url and media.thumbnail_url != media.media_url:
        assets.append(
            SourceMediaAsset(
                kind=SourceMediaKind.IMAGE,
                url=media.thumbnail_url,
                position=len(assets),
                metadata={"source": "threads_api_thumbnail", "media_id": media.id},
            )
        )
    return assets


def _quoted_text(media: ThreadsMedia) -> str | None:
    for key, value in (("Quoted", media.quoted_post), ("Reposted", media.reposted_post)):
        if not isinstance(value, dict):
            continue
        username = value.get("username")
        text = value.get("text")
        parts = [str(part) for part in (username, text) if isinstance(part, str) and part.strip()]
        if parts:
            return f"{key} context: {' - '.join(parts)}"
    return None


def _media_kind(media_type: str | None) -> SourceMediaKind:
    lowered = (media_type or "").lower()
    if "video" in lowered:
        return SourceMediaKind.VIDEO
    return SourceMediaKind.IMAGE


def _response_metadata(response: httpx.Response, media_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "api_status": str(response.status_code),
        "provider_resource_id": media_id,
    }
    reset = response.headers.get("x-business-use-case-usage") or response.headers.get(
        "x-rate-limit-reset"
    )
    if reset:
        metadata["rate_limit"] = {"reset": reset}
    return metadata


def _safe_attempt_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "api_status",
        "auth_strategy",
        "connection_id",
        "provider_resource_id",
        "rate_limit",
    }
    return {key: value for key, value in metadata.items() if key in allowed}


def _error_code_for_status(status_code: int) -> str:
    return {
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        429: "rate_limited",
    }.get(status_code, "api_error")
