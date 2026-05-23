"""Authenticated Instagram API extraction tier."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.adapters.social.meta import InstagramOAuthError
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
from app.core.urls.meta import extract_instagram_shortcode
from app.core.url_utils import normalize_url
from app.domain.models.source import SourceItem, SourceKind

if TYPE_CHECKING:
    from app.adapters.social.meta import InstagramClient, InstagramMedia
    from app.application.ports.social_connections import (
        SocialConnectionRecord,
        SocialConnectionRepositoryPort,
    )


@dataclass(frozen=True, slots=True)
class InstagramApiExtractionResult:
    ok: bool
    content_text: str = ""
    content_source: str = "none"
    title: str | None = None
    images: list[str] | None = None
    metadata: dict[str, Any] | None = None
    source_item: SourceItem | None = None
    normalized_document: NormalizedSourceDocument | None = None
    detected_lang: str | None = None


class InstagramApiExtractor:
    """Use connected Instagram credentials only for media owned by that account."""

    def __init__(
        self,
        *,
        repository: SocialConnectionRepositoryPort,
        instagram_client: InstagramClient,
        media_lookup_page_limit: int = 5,
        token_resolver: SocialAccessTokenResolver | None = None,
    ) -> None:
        self._repository = repository
        self._instagram_client = instagram_client
        self._media_lookup_page_limit = media_lookup_page_limit
        self._token_resolver = token_resolver or SocialAccessTokenResolver(
            repository=repository,
            oauth_clients={"instagram": instagram_client},
        )

    async def extract(
        self,
        *,
        url: str,
        kind_hint: SourceKind,
        user_id: int | None,
        request_id: int | None,
        dedupe_hash: str,
    ) -> InstagramApiExtractionResult:
        shortcode = extract_instagram_shortcode(url)
        base_metadata: dict[str, Any] = {
            "source": "meta",
            "platform": "instagram",
            "platform_surface": kind_hint.value,
            "source_url": url,
            "normalized_url": _normalize_url_or_none(url),
            "provider_shortcode": shortcode,
            "api_status": "skipped",
            "api_supported_for_url": False,
            "unsupported_reason": "no_supported_shortcode_lookup",
            "auth_strategy": {
                "authenticated_supported": True,
                "selected_tier": "meta_scraper_fallback",
            },
        }
        if not shortcode:
            return InstagramApiExtractionResult(ok=False, metadata=base_metadata)
        if user_id is None:
            base_metadata["unsupported_reason"] = "no_user_context"
            return InstagramApiExtractionResult(ok=False, metadata=base_metadata)

        token = await self._token_resolver.resolve(
            user_id=user_id,
            provider="instagram",
            required_scopes=("instagram_business_basic",),
        )
        if token.status in {"skipped", "no_connection"}:
            base_metadata["api_status"] = "no_connection"
            base_metadata["unsupported_reason"] = "no_active_connection"
            await self._record_attempt(None, user_id, "failed", base_metadata, "no_connection")
            return InstagramApiExtractionResult(ok=False, metadata=base_metadata)

        connection = token.connection
        if not token.ok or connection is None or token.access_token is None:
            base_metadata.update(token.safe_metadata())
            base_metadata["unsupported_reason"] = (
                "token_refresh_failed" if token.status == "refresh_failed" else token.status
            )
            if connection is not None:
                await self._record_attempt(
                    connection, user_id, "failed", base_metadata, token.status
                )
            return InstagramApiExtractionResult(ok=False, metadata=base_metadata)

        access_token = token.access_token.get_secret_value()
        metadata = {**base_metadata, "connection_id": connection.id}
        try:
            ig_user_id = token.provider_user_id or await self._get_current_user_id(access_token)
            if not ig_user_id:
                metadata["api_status"] = "unsupported"
                metadata["unsupported_reason"] = "connected_account_id_unavailable"
                await self._record_attempt(
                    connection,
                    user_id,
                    "failed",
                    metadata,
                    "unsupported",
                )
                return InstagramApiExtractionResult(ok=False, metadata=metadata)
            media_payload = await self._resolve_owned_media_payload(
                ig_user_id=ig_user_id,
                shortcode=shortcode,
                access_token=access_token,
                metadata=metadata,
            )
        except InstagramOAuthError as exc:
            await self._handle_oauth_failure(connection, user_id, metadata, exc)
            return InstagramApiExtractionResult(ok=False, metadata=metadata)

        if media_payload is None:
            metadata["api_status"] = "unsupported"
            metadata["unsupported_reason"] = "not_connected_account_media"
            await self._record_attempt(connection, user_id, "failed", metadata, "unsupported")
            return InstagramApiExtractionResult(ok=False, metadata=metadata)

        from app.adapters.social.meta import InstagramMedia

        media = InstagramMedia.from_payload(media_payload)
        result = _build_result_from_media(
            media=media,
            url=url,
            kind_hint=kind_hint,
            request_id=request_id,
            dedupe_hash=dedupe_hash,
            metadata={
                **metadata,
                "api_status": "ok",
                "api_supported_for_url": True,
                "unsupported_reason": None,
            },
        )
        if not result.ok:
            await self._record_attempt(
                connection,
                user_id,
                "failed",
                result.metadata or metadata,
                "empty",
            )
            return result
        await self._record_attempt(
            connection,
            user_id,
            "succeeded",
            result.metadata or metadata,
            None,
        )
        return result

    async def _get_current_user_id(self, access_token: str) -> str | None:
        me = await self._instagram_client.get_me(access_token=access_token)
        user_id = me.get("user_id")
        return user_id if isinstance(user_id, str) and user_id else None

    async def _resolve_owned_media_payload(
        self,
        *,
        ig_user_id: str,
        shortcode: str,
        access_token: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        after: str | None = None
        for _ in range(self._media_lookup_page_limit):
            page = await self._instagram_client.get_user_media_ids(
                ig_user_id,
                access_token=access_token,
                limit=100,
                after=after,
            )
            media_ids = [
                str(item["id"])
                for item in page.get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
            metadata["media_lookup_count"] = metadata.get("media_lookup_count", 0) + len(media_ids)
            for media_id in media_ids:
                payload = await self._instagram_client.get_media_payload(
                    media_id,
                    access_token=access_token,
                )
                if _shortcode_from_permalink(payload.get("permalink")) == shortcode:
                    metadata["provider_resource_id"] = str(payload.get("id") or media_id)
                    return payload
            paging = page.get("paging") if isinstance(page.get("paging"), dict) else {}
            cursors = paging.get("cursors") if isinstance(paging.get("cursors"), dict) else {}
            next_after = cursors.get("after")
            if not isinstance(next_after, str) or not next_after:
                break
            after = next_after
        return None

    async def _handle_oauth_failure(
        self,
        connection: SocialConnectionRecord,
        user_id: int,
        metadata: dict[str, Any],
        exc: InstagramOAuthError,
    ) -> None:
        metadata["api_status"] = str(exc.status_code)
        metadata["unsupported_reason"] = "api_request_failed"
        error_code = _error_code_for_status(exc.status_code)
        if exc.status_code == 401:
            metadata["unsupported_reason"] = "token_invalid"
            await self._token_resolver.mark_needs_reauth(user_id=user_id, provider="instagram")
        await self._record_attempt(connection, user_id, "failed", metadata, error_code)

    async def _record_attempt(
        self,
        connection: SocialConnectionRecord | None,
        user_id: int,
        status: str,
        metadata: dict[str, Any],
        error_code: str | None,
    ) -> None:
        await self._repository.record_fetch_attempt(
            SocialFetchAttemptCreate(
                user_id=user_id,
                provider="instagram",
                connection_id=connection.id if connection is not None else None,
                attempt_type="media_retrieval",
                status=status,
                error_code=error_code,
                error_message=error_code,
                source_url=_string(metadata.get("source_url")),
                normalized_url=_string(metadata.get("normalized_url")),
                provider_resource_id=_string(metadata.get("provider_resource_id")),
                http_status=_http_status(metadata.get("api_status")),
                auth_tier=_selected_tier(metadata),
                metadata_json=_safe_attempt_metadata(metadata),
            )
        )


def _build_result_from_media(
    *,
    media: InstagramMedia,
    url: str,
    kind_hint: SourceKind,
    request_id: int | None,
    dedupe_hash: str,
    metadata: dict[str, Any],
) -> InstagramApiExtractionResult:
    caption = media.caption or ""
    content_parts = [
        caption,
        f"Permalink: {media.permalink}" if media.permalink else None,
        f"Posted at: {media.timestamp}" if media.timestamp else None,
    ]
    content_text = "\n\n".join(part for part in content_parts if part)
    media_assets = _media_assets(media)
    if not content_text and not media_assets:
        return InstagramApiExtractionResult(
            ok=False,
            metadata={**metadata, "api_status": "empty", "unsupported_reason": "empty_media"},
        )

    source_kind = _source_kind(media, kind_hint)
    source_item = SourceItem.create(
        kind=source_kind,
        original_value=url,
        normalized_value=url,
        external_id=media.id,
        request_id=request_id,
        title_hint=None,
        metadata={
            "platform": "meta",
            "platform_surface": source_kind.value,
            "dedupe_hash": dedupe_hash,
        },
    )
    text_blocks: list[SourceTextBlock] = []
    if caption:
        text_blocks.append(
            SourceTextBlock(kind=ExtractedTextKind.CAPTION, text=caption, position=0)
        )
    detected_lang = detect_language(content_text or caption)
    result_metadata = {
        **metadata,
        "platform_surface": source_kind.value,
        "auth_strategy": {
            "authenticated_supported": True,
            "selected_tier": "instagram_api",
        },
        "instagram_media": media.to_dict(),
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
            extraction_source="instagram_api",
            metadata={"dedupe_hash": dedupe_hash},
        ),
    )
    return InstagramApiExtractionResult(
        ok=True,
        content_text=content_text,
        content_source="instagram_api",
        images=[
            asset.url for asset in media_assets if asset.kind == SourceMediaKind.IMAGE and asset.url
        ],
        metadata=result_metadata,
        source_item=source_item,
        normalized_document=normalized,
        detected_lang=detected_lang,
    )


def _source_kind(media: InstagramMedia, kind_hint: SourceKind) -> SourceKind:
    if (media.media_type or "").upper() == "CAROUSEL_ALBUM":
        return SourceKind.INSTAGRAM_CAROUSEL
    if kind_hint == SourceKind.INSTAGRAM_REEL:
        return SourceKind.INSTAGRAM_REEL
    return SourceKind.INSTAGRAM_POST


def _media_assets(media: InstagramMedia) -> list[SourceMediaAsset]:
    assets: list[SourceMediaAsset] = []
    if media.media_url:
        assets.append(
            SourceMediaAsset(
                kind=_media_kind(media.media_type),
                url=media.media_url,
                position=len(assets),
                alt_text=media.alt_text,
                metadata={"source": "instagram_api", "media_id": media.id},
            )
        )
    if media.thumbnail_url and media.thumbnail_url != media.media_url:
        assets.append(
            SourceMediaAsset(
                kind=SourceMediaKind.IMAGE,
                url=media.thumbnail_url,
                position=len(assets),
                metadata={"source": "instagram_api_thumbnail", "media_id": media.id},
            )
        )
    children = media.children.get("data") if isinstance(media.children, dict) else None
    if isinstance(children, list):
        for item in children:
            if not isinstance(item, dict):
                continue
            url = item.get("media_url") or item.get("thumbnail_url")
            if not isinstance(url, str) or not url:
                continue
            media_type = item.get("media_type") if isinstance(item.get("media_type"), str) else None
            assets.append(
                SourceMediaAsset(
                    kind=_media_kind(media_type),
                    url=url,
                    position=len(assets),
                    metadata={"source": "instagram_api_child", "media_id": item.get("id")},
                )
            )
    return assets


def _media_kind(media_type: str | None) -> SourceMediaKind:
    lowered = (media_type or "").lower()
    if "video" in lowered:
        return SourceMediaKind.VIDEO
    return SourceMediaKind.IMAGE


def _shortcode_from_permalink(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return extract_instagram_shortcode(value)


def _safe_attempt_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "api_status",
        "api_supported_for_url",
        "auth_strategy",
        "connection_id",
        "media_lookup_count",
        "normalized_url",
        "provider_resource_id",
        "provider_shortcode",
        "source_url",
        "unsupported_reason",
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


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


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
