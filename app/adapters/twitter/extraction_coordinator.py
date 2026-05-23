"""Shared mode-aware Twitter extraction coordinator."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.adapters.content.platform_extraction.models import (
    PlatformExtractionRequest,
    PlatformExtractionResult,
)
from app.adapters.twitter.article_link_resolver import (
    TwitterArticleLinkResolution,
    resolve_twitter_article_link,
)
from app.application.dto.aggregation import (
    NormalizedSourceDocument,
    SourceMediaAsset,
    SourceMediaKind,
)
from app.core.lang import detect_language
from app.core.logging_utils import get_logger
from app.core.url_utils import (
    canonicalize_twitter_url,
    compute_dedupe_hash,
    extract_tweet_id,
    extract_twitter_article_id,
    is_twitter_article_url,
)
from app.domain.models.source import SourceItem, SourceKind
from app.observability.failure_observability import (
    REASON_EXTRACTION_EMPTY_OUTPUT,
    REASON_RESOLVE_FAILED,
    persist_request_failure,
)
from app.observability.metrics import record_twitter_article_resolution

if TYPE_CHECKING:
    from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.twitter.firecrawl_extractor import TwitterFirecrawlExtractor
    from app.adapters.twitter.api_extractor import XApiPostExtractor
    from app.adapters.twitter.playwright_extractor import TwitterPlaywrightExtractor
    from app.adapters.twitter.tier_policy import TwitterTierPolicy

logger = get_logger(__name__)
_ARTICLE_REDIRECT_HOSTS = {
    "x.com",
    "twitter.com",
    "www.x.com",
    "www.twitter.com",
    "mobile.x.com",
    "mobile.twitter.com",
    "t.co",
}


class TwitterExtractionCoordinator:
    """Single mode-aware coordinator for interactive and pure Twitter extraction."""

    def __init__(
        self,
        *,
        cfg: Any,
        response_formatter: ResponseFormatter,
        request_repo: Any,
        lifecycle: PlatformRequestLifecycle,
        tier_policy: TwitterTierPolicy,
        x_api_extractor: XApiPostExtractor | None = None,
        firecrawl_extractor: TwitterFirecrawlExtractor,
        playwright_extractor: TwitterPlaywrightExtractor,
    ) -> None:
        self._cfg = cfg
        self._response_formatter = response_formatter
        self._request_repo = request_repo
        self._lifecycle = lifecycle
        self._tier_policy = tier_policy
        self._x_api_extractor = x_api_extractor
        self._firecrawl_extractor = firecrawl_extractor
        self._playwright_extractor = playwright_extractor

    async def extract(self, request: PlatformExtractionRequest) -> PlatformExtractionResult:
        tweet_id = extract_tweet_id(request.url_text)
        is_article = is_twitter_article_url(request.url_text)
        article_id = extract_twitter_article_id(request.url_text) if is_article else None

        extraction_url, article_resolution = await self._resolve_article_link(
            url_text=request.url_text,
            tweet_id=tweet_id,
            is_article=is_article,
            correlation_id=request.correlation_id,
        )
        if article_resolution.is_article:
            is_article = True
            article_id = article_resolution.article_id or article_id

        dedupe_source = (
            article_resolution.canonical_url or article_resolution.resolved_url or request.url_text
        )
        dedupe = compute_dedupe_hash(dedupe_source if is_article else request.url_text)
        req_id = request.request_id_override
        if request.mode == "interactive":
            await self._lifecycle.send_accepted_notification(request)
            req_id = await self._lifecycle.handle_request_dedupe_or_create(
                request,
                dedupe_hash=dedupe,
            )

        metadata = self._build_twitter_metadata(
            tweet_id=tweet_id,
            is_article=is_article,
            article_id=article_id,
            tier_mode=self._tier_policy.force_tier(),
            article_resolution=article_resolution,
        )
        content_text = ""
        content_source = "none"
        x_api_ok = False

        if self._x_api_extractor is not None and not is_article:
            x_api_result = await self._x_api_extractor.extract(
                url_text=extraction_url,
                user_id=request.user_id,
                correlation_id=request.correlation_id,
                metadata=metadata,
            )
            metadata.update(x_api_result.metadata)
            metadata["tier_outcomes"]["x_api"] = "success" if x_api_result.ok else "failed"
            if x_api_result.ok:
                content_text = x_api_result.content_text
                content_source = x_api_result.content_source
                x_api_ok = True
                metadata["auth_strategy"] = {"selected_tier": "x_api"}
            elif x_api_result.metadata.get("api_status") in {"skipped", "no_connection"}:
                metadata["tier_outcomes"]["x_api"] = "skipped"
        else:
            metadata["tier_outcomes"]["x_api"] = "disabled" if is_article else "unconfigured"

        use_firecrawl_tier = self._tier_policy.should_use_firecrawl_tier()
        use_playwright_tier = self._tier_policy.should_use_playwright_tier()
        if not x_api_ok and not use_firecrawl_tier and not use_playwright_tier:
            raise ValueError(self._tier_policy.build_extraction_error_message())
        firecrawl_ok = False
        if x_api_ok:
            metadata["tier_outcomes"]["firecrawl"] = "skipped"
        elif use_firecrawl_tier:
            firecrawl_ok, content_text, content_source = await self._firecrawl_extractor.extract(
                url_text=extraction_url,
                req_id=req_id,
                tweet_id=tweet_id,
                metadata=metadata,
                correlation_id=request.correlation_id,
                is_article=is_article,
                persist_result=request.mode == "interactive",
            )
            metadata["tier_outcomes"]["firecrawl"] = "success" if firecrawl_ok else "failed"
            if firecrawl_ok:
                metadata["auth_strategy"] = {"selected_tier": "firecrawl"}
        else:
            metadata["tier_outcomes"]["firecrawl"] = (
                "forced_skip" if self._tier_policy.force_tier() == "playwright" else "disabled"
            )

        should_try_playwright = not x_api_ok and use_playwright_tier and (
            self._tier_policy.force_tier() == "playwright"
            or not firecrawl_ok
            or self._should_enrich_with_playwright_media(
                is_article=is_article,
                firecrawl_ok=firecrawl_ok,
            )
        )
        if should_try_playwright:
            try:
                pw_text, pw_source, pw_metadata = await self._playwright_extractor.extract(
                    url_text=extraction_url,
                    tweet_id=tweet_id,
                    is_article=is_article,
                    correlation_id=request.correlation_id,
                    metadata=metadata,
                    timeout_ms=self._tier_policy.effective_timeout_ms(),
                    request_id=req_id,
                )
                content_text = pw_text
                content_source = pw_source
                if pw_metadata is not metadata:
                    metadata.update(pw_metadata)
                metadata["tier_outcomes"]["playwright"] = "success"
                metadata["auth_strategy"] = {"selected_tier": "playwright"}
            except Exception as exc:
                metadata["tier_outcomes"]["playwright"] = "failed"
                logger.warning(
                    "twitter_playwright_failed",
                    extra={
                        "cid": request.correlation_id,
                        "error": str(exc),
                        "tweet_id": tweet_id,
                        "article_id": article_id,
                    },
                )
                if self._tier_policy.force_tier() == "playwright":
                    raise
        elif not use_playwright_tier:
            metadata["tier_outcomes"]["playwright"] = (
                "forced_skip" if self._tier_policy.force_tier() == "firecrawl" else "disabled"
            )

        if not content_text:
            await self._handle_empty_output(
                request=request,
                req_id=req_id,
                url_text=request.url_text,
                metadata=metadata,
                is_article=is_article,
            )

        detected = detect_language(content_text)
        if request.mode == "interactive" and req_id is not None:
            await self._lifecycle.persist_detected_lang(req_id, detected)
        source_kind = SourceKind.X_ARTICLE if is_article else SourceKind.X_POST
        source_item = SourceItem.create(
            kind=source_kind,
            original_value=request.url_text,
            normalized_value=metadata.get("article_canonical_url") or request.normalized_url,
            external_id=article_id if is_article else tweet_id,
            request_id=req_id,
            title_hint=metadata.get("title"),
            metadata={
                "platform": "twitter",
                "article_resolution_reason": metadata.get("article_resolution_reason"),
            },
        )
        media_assets = self._build_media_assets(metadata)
        normalized_document = NormalizedSourceDocument.from_extracted_content(
            source_item=source_item,
            text=content_text,
            title=metadata.get("title"),
            detected_language=detected,
            content_source=content_source,
            media_assets=media_assets,
            metadata=metadata,
        )
        return PlatformExtractionResult(
            platform="twitter",
            request_id=req_id,
            content_text=content_text,
            content_source=content_source,
            detected_lang=detected,
            title=metadata.get("title"),
            images=[asset.url for asset in media_assets if asset.url],
            metadata=metadata,
            source_item=source_item,
            normalized_document=normalized_document,
        )

    def _build_twitter_metadata(
        self,
        *,
        tweet_id: str | None,
        is_article: bool,
        article_id: str | None,
        tier_mode: str,
        article_resolution: TwitterArticleLinkResolution,
    ) -> dict[str, Any]:
        return {
            "source": "twitter",
            "tweet_id": tweet_id,
            "is_article": is_article,
            "article_id": article_id,
            "tier_mode": tier_mode,
            "article_resolution_reason": article_resolution.reason,
            "article_resolved_url": article_resolution.resolved_url,
            "article_canonical_url": article_resolution.canonical_url,
            "article_extraction_stage": None,
            "auth_strategy": {"selected_tier": None},
            "api_status": None,
            "provider_resource_id": tweet_id or article_id,
            "tier_outcomes": {"x_api": "skipped", "firecrawl": "skipped", "playwright": "skipped"},
        }

    def _should_enrich_with_playwright_media(
        self,
        *,
        is_article: bool,
        firecrawl_ok: bool,
    ) -> bool:
        if self._tier_policy.force_tier() == "firecrawl":
            return False
        return firecrawl_ok and not is_article

    def _build_media_assets(self, metadata: dict[str, Any]) -> list[SourceMediaAsset]:
        if not bool(
            getattr(getattr(self._cfg, "runtime", None), "aggregation_article_media_enabled", True)
        ):
            return []
        tweet_media = metadata.get("tweet_media")
        if isinstance(tweet_media, list):
            assets = self._build_tweet_media_assets(tweet_media)
            if assets:
                return assets

        article_images = metadata.get("article_images")
        if isinstance(article_images, list):
            return [
                SourceMediaAsset(
                    kind=SourceMediaKind.IMAGE,
                    url=image_url,
                    position=index,
                    metadata={"platform": "twitter", "source": "article_image"},
                )
                for index, image_url in enumerate(article_images)
                if isinstance(image_url, str) and image_url.strip()
            ]
        return []

    def _build_tweet_media_assets(
        self,
        tweet_media: list[Any],
    ) -> list[SourceMediaAsset]:
        assets: list[SourceMediaAsset] = []
        seen_urls: set[str] = set()
        for item in tweet_media:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            assets.append(
                SourceMediaAsset(
                    kind=SourceMediaKind.IMAGE,
                    url=url,
                    alt_text=str(item.get("alt_text") or "").strip() or None,
                    position=len(assets),
                    metadata={
                        "platform": "twitter",
                        "tweet_id": item.get("tweet_id"),
                        "tweet_author_handle": item.get("tweet_author_handle"),
                        "tweet_order": item.get("tweet_order"),
                        "media_index": item.get("media_index"),
                        "from_quoted_post": bool(item.get("from_quoted_post")),
                        "quoted_by_tweet_id": item.get("quoted_by_tweet_id"),
                    },
                )
            )
        return assets

    async def _handle_empty_output(
        self,
        *,
        request: PlatformExtractionRequest,
        req_id: int | None,
        url_text: str,
        metadata: dict[str, Any],
        is_article: bool,
    ) -> None:
        error_msg = self._tier_policy.build_extraction_error_message()
        reason_code = (
            REASON_RESOLVE_FAILED
            if metadata.get("article_resolution_reason") == "resolve_failed"
            else REASON_EXTRACTION_EMPTY_OUTPUT
        )
        if req_id is not None:
            await persist_request_failure(
                request_repo=self._request_repo,
                logger=logger,
                request_id=req_id,
                correlation_id=request.correlation_id,
                stage="extraction",
                component="platform_router",
                reason_code=reason_code,
                error=ValueError(error_msg),
                retryable=True,
                source_url=url_text,
                resolved_url=metadata.get("article_resolved_url"),
                canonical_url=metadata.get("article_canonical_url"),
                article_id=metadata.get("article_id"),
                content_signals={
                    "tier_outcomes": metadata.get("tier_outcomes"),
                    "is_article": is_article,
                },
            )
        if request.mode == "interactive" and request.message is not None:
            await self._response_formatter.send_error_notification(
                request.message,
                "twitter_extraction_error",
                request.correlation_id,
                details=error_msg,
            )
        raise ValueError(error_msg)

    async def _resolve_article_link(
        self,
        *,
        url_text: str,
        tweet_id: str | None,
        is_article: bool,
        correlation_id: str | None,
    ) -> tuple[str, TwitterArticleLinkResolution]:
        default_result = TwitterArticleLinkResolution(
            input_url=url_text,
            resolved_url=url_text if is_article else None,
            canonical_url=canonicalize_twitter_url(url_text) if is_article else None,
            article_id=extract_twitter_article_id(url_text) if is_article else None,
            is_article=is_article,
            reason="path_match" if is_article else "not_article",
        )
        if is_article:
            record_twitter_article_resolution(status="hit", reason="path_match")
            return default_result.canonical_url or url_text, default_result
        if tweet_id:
            return url_text, default_result
        if not self._cfg.twitter.article_redirect_resolution_enabled:
            return url_text, default_result
        host = (self._safe_hostname(url_text) or "").lower()
        if host not in _ARTICLE_REDIRECT_HOSTS:
            return url_text, default_result

        logger.info(
            "twitter_article_resolution_attempt",
            extra={"cid": correlation_id, "url": url_text, "host": host},
        )
        started = time.perf_counter()
        resolution = await resolve_twitter_article_link(
            url_text,
            timeout_s=self._cfg.twitter.article_resolution_timeout_sec,
        )
        elapsed = max(0.0, time.perf_counter() - started)
        status = "hit" if resolution.is_article else "miss"
        if resolution.reason == "resolve_failed":
            status = "error"
        record_twitter_article_resolution(
            status=status,
            reason=resolution.reason,
            latency_seconds=elapsed,
        )
        logger.info(
            "twitter_article_resolution_result",
            extra={
                "cid": correlation_id,
                "reason": resolution.reason,
                "is_article": resolution.is_article,
                "resolved_url": resolution.resolved_url,
                "canonical_url": resolution.canonical_url,
                "article_id": resolution.article_id,
                "latency_ms": int(elapsed * 1000),
            },
        )
        if resolution.is_article:
            resolved_target = resolution.canonical_url or resolution.resolved_url or url_text
            return resolved_target, resolution
        return url_text, resolution

    @staticmethod
    def _safe_hostname(url: str) -> str | None:
        try:
            candidate = url.strip()
            if "://" not in candidate:
                candidate = f"https://{candidate}"
            return urlparse(candidate).hostname
        except Exception:
            return None
