"""Platform extractor for Threads and Instagram content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.adapters.content.platform_extraction.models import (
    PlatformExtractionRequest,
    PlatformExtractionResult,
)
from app.adapters.content.platform_extraction.protocol import PlatformExtractor
from app.adapters.video.source_extractor import (
    MetadataDrivenVideoSourceExtractor,
    VideoSourceRequest,
    default_video_controls,
)
from app.application.dto.aggregation import (
    ExtractedTextKind,
    NormalizedSourceDocument,
    SourceMediaAsset,
    SourceMediaKind,
    SourceProvenance,
    SourceTextBlock,
)
from app.core.call_status import CallStatus
from app.core.html_utils import clean_markdown_article_text, html_to_text
from app.core.lang import detect_language
from app.core.url_utils import compute_dedupe_hash
from app.core.urls.meta import (
    extract_instagram_shortcode,
    extract_threads_post_id,
    is_instagram_post_url,
    is_instagram_reel_url,
    is_instagram_url,
    is_threads_url,
)
from app.domain.models.source import SourceItem, SourceKind

if TYPE_CHECKING:
    from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
    from app.adapters.meta.instagram_api_extractor import InstagramApiExtractor
    from app.adapters.meta.threads_api_extractor import ThreadsApiExtractor
    from app.config import AppConfig

_META_LOGIN_WALL_TERMS = (
    "log in",
    "login",
    "sign up",
    "see instagram photos and videos",
    "continue to view profile",
    "get the app",
)


@dataclass(slots=True, frozen=True)
class _MetaBuildResult:
    kind: SourceKind
    title: str | None
    text: str
    text_blocks: list[SourceTextBlock]
    media: list[SourceMediaAsset]
    metadata: dict[str, Any]
    content_source: str


class MetaPlatformExtractor(PlatformExtractor):
    """First-class extractor for Threads and Instagram URLs."""

    def __init__(
        self,
        *,
        cfg: AppConfig | Any,
        scraper: Any,
        firecrawl_sem: Any,
        lifecycle: PlatformRequestLifecycle,
        threads_api_extractor: ThreadsApiExtractor | None = None,
        instagram_api_extractor: InstagramApiExtractor | None = None,
    ) -> None:
        self._cfg = cfg
        self._scraper = scraper
        self._firecrawl_sem = firecrawl_sem
        self._lifecycle = lifecycle
        self._video_source_extractor = MetadataDrivenVideoSourceExtractor()
        self._threads_api_extractor = threads_api_extractor
        self._instagram_api_extractor = instagram_api_extractor

    def supports(self, normalized_url: str) -> bool:
        return is_threads_url(normalized_url) or is_instagram_url(normalized_url)

    async def extract(self, request: PlatformExtractionRequest) -> PlatformExtractionResult:
        kind_hint = _classify_meta_source_kind(request.normalized_url)
        dedupe_hash = compute_dedupe_hash(request.normalized_url)
        request_id = request.request_id_override
        if request.mode == "interactive":
            await self._lifecycle.send_accepted_notification(request)
            request_id = await self._lifecycle.handle_request_dedupe_or_create(
                request,
                dedupe_hash=dedupe_hash,
            )

        threads_api_metadata: dict[str, Any] | None = None
        if kind_hint == SourceKind.THREADS_POST and self._threads_api_extractor is not None:
            threads_api_result = await self._threads_api_extractor.extract(
                url=request.normalized_url,
                user_id=request.user_id,
                request_id=request_id,
                dedupe_hash=dedupe_hash,
            )
            if threads_api_result.ok:
                detected_lang = threads_api_result.detected_lang or detect_language(
                    threads_api_result.content_text
                )
                if request.mode == "interactive" and request_id is not None:
                    await self._lifecycle.persist_detected_lang(request_id, detected_lang)
                return PlatformExtractionResult(
                    platform="meta",
                    request_id=request_id,
                    content_text=threads_api_result.content_text,
                    content_source=threads_api_result.content_source,
                    detected_lang=detected_lang,
                    title=threads_api_result.title,
                    images=threads_api_result.images or [],
                    metadata=threads_api_result.metadata or {},
                    source_item=threads_api_result.source_item,
                    normalized_document=threads_api_result.normalized_document,
                )
            threads_api_metadata = threads_api_result.metadata

        instagram_api_metadata: dict[str, Any] | None = None
        if (
            kind_hint
            in {
                SourceKind.INSTAGRAM_POST,
                SourceKind.INSTAGRAM_REEL,
            }
            and self._instagram_api_extractor is not None
        ):
            instagram_api_result = await self._instagram_api_extractor.extract(
                url=request.normalized_url,
                kind_hint=kind_hint,
                user_id=request.user_id,
                request_id=request_id,
                dedupe_hash=dedupe_hash,
            )
            if instagram_api_result.ok:
                detected_lang = instagram_api_result.detected_lang or detect_language(
                    instagram_api_result.content_text
                )
                if request.mode == "interactive" and request_id is not None:
                    await self._lifecycle.persist_detected_lang(request_id, detected_lang)
                return PlatformExtractionResult(
                    platform="meta",
                    request_id=request_id,
                    content_text=instagram_api_result.content_text,
                    content_source=instagram_api_result.content_source,
                    detected_lang=detected_lang,
                    title=instagram_api_result.title,
                    images=instagram_api_result.images or [],
                    metadata=instagram_api_result.metadata or {},
                    source_item=instagram_api_result.source_item,
                    normalized_document=instagram_api_result.normalized_document,
                )
            instagram_api_metadata = instagram_api_result.metadata

        async with self._firecrawl_sem():
            crawl = await self._scraper.scrape_markdown(
                request.normalized_url,
                request_id=request_id,
            )

        build_result = _build_meta_document(
            url=request.normalized_url,
            kind_hint=kind_hint,
            request_id=request_id,
            crawl=crawl,
            api_fallback_metadata=threads_api_metadata or instagram_api_metadata,
        )

        detected_lang = detect_language(
            "\n".join(block.text for block in build_result.text_blocks) or build_result.text
        )
        source_item = SourceItem.create(
            kind=build_result.kind,
            original_value=request.url_text,
            normalized_value=request.normalized_url,
            external_id=_extract_external_id(request.normalized_url, build_result.kind),
            request_id=request_id,
            title_hint=build_result.title,
            metadata={
                "platform": "meta",
                "platform_surface": build_result.kind.value,
                "dedupe_hash": dedupe_hash,
            },
        )
        if any(asset.kind == SourceMediaKind.VIDEO for asset in build_result.media) and bool(
            getattr(
                getattr(self._cfg, "runtime", None),
                "aggregation_non_youtube_video_enabled",
                True,
            )
        ):
            transcript_text = _extract_block_text(
                build_result.text_blocks, ExtractedTextKind.TRANSCRIPT
            )
            audio_transcript_text = _extract_audio_transcript(
                build_result.metadata.get("firecrawl_metadata")
            )
            video_result = self._video_source_extractor.extract(
                VideoSourceRequest(
                    source_item=source_item,
                    platform="meta",
                    title=build_result.title,
                    body_text=_extract_body_like_text(build_result.text_blocks),
                    body_kind=(
                        ExtractedTextKind.CAPTION
                        if source_item.kind
                        in {
                            SourceKind.INSTAGRAM_POST,
                            SourceKind.INSTAGRAM_CAROUSEL,
                        }
                        else ExtractedTextKind.BODY
                    ),
                    transcript_text=transcript_text,
                    transcript_source=(
                        "audio_transcript"
                        if transcript_text
                        and audio_transcript_text
                        and transcript_text == audio_transcript_text
                        else None
                    ),
                    audio_transcript_text=(
                        None
                        if transcript_text
                        and audio_transcript_text
                        and transcript_text == audio_transcript_text
                        else audio_transcript_text
                    ),
                    ocr_text=_extract_block_text(build_result.text_blocks, ExtractedTextKind.OCR),
                    content_source=build_result.content_source,
                    detected_language=detected_lang,
                    additional_text_blocks=tuple(
                        block
                        for block in build_result.text_blocks
                        if block.metadata.get("role") == "quoted_context"
                    ),
                    existing_media=tuple(build_result.media),
                    primary_video_url=_extract_primary_video_url(build_result.media),
                    metadata=build_result.metadata,
                    controls=default_video_controls(),
                )
            )
            normalized_document = video_result.normalized_document
            metadata = dict(video_result.metadata)
            metadata["request_id"] = request_id
            metadata["detected_lang"] = normalized_document.detected_language
            content_text = video_result.content_text
            content_source = video_result.content_source
            images = video_result.images
            detected_lang = normalized_document.detected_language or detected_lang
        else:
            normalized_document = NormalizedSourceDocument(
                source_item_id=source_item.stable_id,
                source_kind=source_item.kind,
                title=build_result.title,
                text=build_result.text,
                detected_language=detected_lang,
                text_blocks=build_result.text_blocks,
                media=build_result.media,
                metadata=build_result.metadata,
                provenance=SourceProvenance(
                    source_item_id=source_item.stable_id,
                    source_kind=source_item.kind,
                    original_value=source_item.original_value,
                    normalized_value=source_item.normalized_value,
                    external_id=source_item.external_id,
                    request_id=request_id,
                    extraction_source=build_result.content_source,
                    metadata={"dedupe_hash": dedupe_hash},
                ),
            )
            metadata = dict(build_result.metadata)
            if any(asset.kind == SourceMediaKind.VIDEO for asset in build_result.media):
                metadata["video_processing_strategy"] = "disabled_by_runtime_flag"
            metadata["request_id"] = request_id
            metadata["detected_lang"] = detected_lang
            content_text = build_result.text
            content_source = build_result.content_source
            images = [
                asset.url
                for asset in build_result.media
                if asset.kind == SourceMediaKind.IMAGE and asset.url
            ]

        if request.mode == "interactive" and request_id is not None:
            await self._lifecycle.persist_detected_lang(request_id, detected_lang)

        return PlatformExtractionResult(
            platform="meta",
            request_id=request_id,
            content_text=content_text,
            content_source=content_source,
            detected_lang=detected_lang,
            title=build_result.title,
            images=images,
            metadata=metadata,
            source_item=source_item,
            normalized_document=normalized_document,
        )


def _build_meta_document(
    *,
    url: str,
    kind_hint: SourceKind,
    request_id: int | None,
    crawl: Any,
    api_fallback_metadata: dict[str, Any] | None = None,
) -> _MetaBuildResult:
    metadata_json = crawl.metadata_json if isinstance(crawl.metadata_json, dict) else {}
    quality = _evaluate_meta_quality(crawl=crawl, metadata_json=metadata_json)

    title = _clean_text(
        metadata_json.get("title") or metadata_json.get("og:title") or metadata_json.get("headline")
    )
    description = _clean_text(
        metadata_json.get("description")
        or metadata_json.get("og:description")
        or metadata_json.get("caption")
        or metadata_json.get("summary_text")
    )

    primary_text, base_source = _extract_primary_text(crawl)
    primary_text = _clean_text(primary_text)
    fallback_text = description or title or ""
    if quality["reason"] in {"login_wall", "empty"}:
        chosen_text = fallback_text
        content_source = "meta_metadata_fallback"
    else:
        chosen_text = primary_text or fallback_text
        content_source = base_source if primary_text else "meta_metadata_fallback"

    transcript_text = _clean_text(
        metadata_json.get("transcript") or metadata_json.get("audio_transcript")
    )
    ocr_text = _clean_text(
        metadata_json.get("ocr_text")
        or metadata_json.get("frame_text")
        or metadata_json.get("frame_ocr")
    )
    quoted_text = _extract_quoted_text(metadata_json)
    media = _extract_media_assets(metadata_json)
    resolved_kind = _resolve_source_kind(kind_hint, url=url, media=media)
    if not chosen_text and not transcript_text and not ocr_text and not media:
        msg = f"Meta extraction returned no usable content for {url}"
        raise ValueError(msg)

    text_blocks: list[SourceTextBlock] = []
    if title:
        text_blocks.append(
            SourceTextBlock(kind=ExtractedTextKind.TITLE, text=title, position=len(text_blocks))
        )
    if chosen_text:
        block_kind = (
            ExtractedTextKind.CAPTION
            if resolved_kind in {SourceKind.INSTAGRAM_POST, SourceKind.INSTAGRAM_CAROUSEL}
            else ExtractedTextKind.BODY
        )
        text_blocks.append(
            SourceTextBlock(kind=block_kind, text=chosen_text, position=len(text_blocks))
        )
    if quoted_text:
        text_blocks.append(
            SourceTextBlock(
                kind=ExtractedTextKind.BODY,
                text=quoted_text,
                position=len(text_blocks),
                metadata={"role": "quoted_context"},
            )
        )
    if transcript_text:
        text_blocks.append(
            SourceTextBlock(
                kind=ExtractedTextKind.TRANSCRIPT,
                text=transcript_text,
                position=len(text_blocks),
            )
        )
    if ocr_text:
        text_blocks.append(
            SourceTextBlock(kind=ExtractedTextKind.OCR, text=ocr_text, position=len(text_blocks))
        )

    metadata = {
        "source": "meta",
        "platform": "threads" if resolved_kind == SourceKind.THREADS_POST else "instagram",
        "platform_surface": resolved_kind.value,
        "auth_strategy": {
            "selected_tier": content_source,
            "authenticated_supported": False,
            "notes": "Uses unauthenticated scraper extraction with metadata fallback.",
        },
        "quality_checks": quality,
        "firecrawl_metadata": metadata_json,
        "request_id": request_id,
    }
    if api_fallback_metadata and resolved_kind == SourceKind.THREADS_POST:
        metadata = _merge_threads_api_fallback_metadata(metadata, api_fallback_metadata)
    if api_fallback_metadata and resolved_kind in {
        SourceKind.INSTAGRAM_POST,
        SourceKind.INSTAGRAM_CAROUSEL,
        SourceKind.INSTAGRAM_REEL,
    }:
        metadata = _merge_instagram_api_fallback_metadata(
            metadata,
            api_fallback_metadata,
            content_source=content_source,
        )
    return _MetaBuildResult(
        kind=resolved_kind,
        title=title,
        text=chosen_text or transcript_text or ocr_text or "",
        text_blocks=text_blocks,
        media=media,
        metadata=metadata,
        content_source=content_source,
    )


def _merge_threads_api_fallback_metadata(
    scraper_metadata: dict[str, Any],
    api_metadata: dict[str, Any],
) -> dict[str, Any]:
    metadata = {**api_metadata, **scraper_metadata}
    metadata["auth_strategy"] = {
        "authenticated_supported": True,
        "selected_tier": "meta_scraper_fallback",
        "fallback_reason": api_metadata.get("api_status"),
    }
    return metadata


def _merge_instagram_api_fallback_metadata(
    scraper_metadata: dict[str, Any],
    api_metadata: dict[str, Any],
    *,
    content_source: str,
) -> dict[str, Any]:
    metadata = {**api_metadata, **scraper_metadata}
    selected_tier = (
        "metadata_fallback"
        if content_source == "meta_metadata_fallback"
        else "meta_scraper_fallback"
    )
    metadata["api_supported_for_url"] = bool(api_metadata.get("api_supported_for_url"))
    if api_metadata.get("unsupported_reason") is not None:
        metadata["unsupported_reason"] = api_metadata.get("unsupported_reason")
    metadata["auth_strategy"] = {
        "authenticated_supported": True,
        "selected_tier": selected_tier,
        "fallback_reason": api_metadata.get("unsupported_reason") or api_metadata.get("api_status"),
    }
    return metadata


def _classify_meta_source_kind(url: str) -> SourceKind:
    if is_threads_url(url):
        return SourceKind.THREADS_POST
    if is_instagram_reel_url(url):
        return SourceKind.INSTAGRAM_REEL
    if is_instagram_post_url(url):
        return SourceKind.INSTAGRAM_POST
    return SourceKind.WEB_ARTICLE


def _resolve_source_kind(
    kind_hint: SourceKind,
    *,
    url: str,
    media: list[SourceMediaAsset],
) -> SourceKind:
    if kind_hint == SourceKind.INSTAGRAM_POST:
        if sum(1 for item in media if item.kind == SourceMediaKind.IMAGE) > 1:
            return SourceKind.INSTAGRAM_CAROUSEL
        return SourceKind.INSTAGRAM_POST
    if kind_hint == SourceKind.WEB_ARTICLE and is_instagram_reel_url(url):
        return SourceKind.INSTAGRAM_REEL
    return kind_hint


def _evaluate_meta_quality(*, crawl: Any, metadata_json: dict[str, Any]) -> dict[str, Any]:
    raw_text = " ".join(
        part
        for part in (
            _clean_text(crawl.content_markdown),
            _clean_text(crawl.content_html),
            _clean_text(metadata_json.get("title")),
            _clean_text(metadata_json.get("description")),
            _clean_text(metadata_json.get("caption")),
        )
        if part
    ).lower()
    if not raw_text.strip():
        return {"reason": "empty", "login_wall": False}
    login_wall = any(term in raw_text for term in _META_LOGIN_WALL_TERMS)
    return {"reason": "login_wall" if login_wall else "ok", "login_wall": login_wall}


def _extract_primary_text(crawl: Any) -> tuple[str, str]:
    if crawl.status == CallStatus.OK and _clean_text(crawl.content_markdown):
        return clean_markdown_article_text(crawl.content_markdown), "markdown"
    if crawl.status == CallStatus.OK and _clean_text(crawl.content_html):
        return html_to_text(crawl.content_html), "html"
    return "", "none"


def _extract_quoted_text(metadata_json: dict[str, Any]) -> str | None:
    quoted = metadata_json.get("quoted_post")
    if isinstance(quoted, dict):
        author = _clean_text(quoted.get("author") or quoted.get("username"))
        text = _clean_text(quoted.get("text") or quoted.get("caption") or quoted.get("body"))
        if author and text:
            return f"Quoted context from {author}: {text}"
        return text
    return _clean_text(
        metadata_json.get("quoted_text")
        or metadata_json.get("quoted_caption")
        or metadata_json.get("quoted_context")
    )


def _extract_audio_transcript(metadata_json: Any) -> str | None:
    if not isinstance(metadata_json, dict):
        return None
    return _clean_text(metadata_json.get("audio_transcript"))


def _extract_primary_video_url(media: list[SourceMediaAsset]) -> str | None:
    for asset in media:
        if asset.kind == SourceMediaKind.VIDEO and asset.url:
            return asset.url
    return None


def _extract_block_text(
    text_blocks: list[SourceTextBlock],
    kind: ExtractedTextKind,
) -> str | None:
    for block in text_blocks:
        if block.kind == kind:
            return block.text
    return None


def _extract_body_like_text(text_blocks: list[SourceTextBlock]) -> str | None:
    for kind in (ExtractedTextKind.CAPTION, ExtractedTextKind.BODY):
        text = _extract_block_text(text_blocks, kind)
        if text:
            return text
    return None


def _extract_media_assets(metadata_json: dict[str, Any]) -> list[SourceMediaAsset]:
    media_items = metadata_json.get("media")
    if isinstance(media_items, list):
        media_assets = _extract_assets_from_items(media_items)
        if media_assets:
            return media_assets

    assets: list[SourceMediaAsset] = []
    image_candidates = []
    video_candidates = []
    for key in ("images", "image_urls", "thumbnails", "screenshots"):
        image_candidates.extend(_coerce_to_list(metadata_json.get(key)))
    for key in ("videos", "video_urls"):
        video_candidates.extend(_coerce_to_list(metadata_json.get(key)))
    for key in ("image", "image_url", "og:image", "ogImage"):
        image_candidates.extend(_coerce_to_list(metadata_json.get(key)))
    for key in ("video", "video_url", "og:video", "ogVideo"):
        video_candidates.extend(_coerce_to_list(metadata_json.get(key)))

    assets.extend(_extract_assets_from_items(image_candidates, fallback_kind=SourceMediaKind.IMAGE))
    assets.extend(_extract_assets_from_items(video_candidates, fallback_kind=SourceMediaKind.VIDEO))
    return _dedupe_media_assets(assets)


def _extract_assets_from_items(
    items: list[Any],
    *,
    fallback_kind: SourceMediaKind | None = None,
) -> list[SourceMediaAsset]:
    assets: list[SourceMediaAsset] = []
    for item in items:
        url = None
        alt_text = None
        mime_type = None
        kind = fallback_kind
        metadata: dict[str, Any] = {}
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            url = _clean_text(
                item.get("url")
                or item.get("src")
                or item.get("image")
                or item.get("image_url")
                or item.get("video")
                or item.get("video_url")
            )
            alt_text = _clean_text(item.get("alt") or item.get("alt_text"))
            mime_type = _clean_text(item.get("mime_type") or item.get("content_type"))
            metadata = dict(item)
            item_kind = _clean_text(item.get("type") or item.get("kind") or item.get("media_type"))
            if item_kind:
                if "video" in item_kind:
                    kind = SourceMediaKind.VIDEO
                elif "audio" in item_kind:
                    kind = SourceMediaKind.AUDIO
                elif "document" in item_kind:
                    kind = SourceMediaKind.DOCUMENT
                else:
                    kind = SourceMediaKind.IMAGE
        if not url:
            continue
        assets.append(
            SourceMediaAsset(
                kind=kind or _infer_media_kind(url, mime_type),
                url=url,
                position=len(assets),
                alt_text=alt_text,
                mime_type=mime_type,
                metadata=metadata,
            )
        )
    return assets


def _dedupe_media_assets(assets: list[SourceMediaAsset]) -> list[SourceMediaAsset]:
    unique: list[SourceMediaAsset] = []
    seen: set[tuple[SourceMediaKind, str]] = set()
    for asset in assets:
        if not asset.url:
            continue
        key = (asset.kind, asset.url)
        if key in seen:
            continue
        seen.add(key)
        unique.append(asset.model_copy(update={"position": len(unique)}))
    return unique


def _infer_media_kind(url: str, mime_type: str | None) -> SourceMediaKind:
    if mime_type:
        lowered = mime_type.lower()
        if lowered.startswith("video/"):
            return SourceMediaKind.VIDEO
        if lowered.startswith("audio/"):
            return SourceMediaKind.AUDIO
        if lowered.startswith("image/"):
            return SourceMediaKind.IMAGE
    lowered_url = url.lower()
    if any(lowered_url.endswith(ext) for ext in (".mp4", ".mov", ".webm")):
        return SourceMediaKind.VIDEO
    if any(lowered_url.endswith(ext) for ext in (".mp3", ".m4a", ".wav")):
        return SourceMediaKind.AUDIO
    return SourceMediaKind.IMAGE


def _coerce_to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _extract_external_id(url: str, source_kind: SourceKind) -> str | None:
    if source_kind == SourceKind.THREADS_POST:
        return extract_threads_post_id(url)
    if source_kind in {
        SourceKind.INSTAGRAM_POST,
        SourceKind.INSTAGRAM_CAROUSEL,
        SourceKind.INSTAGRAM_REEL,
    }:
        return extract_instagram_shortcode(url)
    return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


__all__ = ["MetaPlatformExtractor"]
