"""Content extraction and processing for URLs."""

# ruff: noqa: E501
# flake8: noqa

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from app.adapters.content.article_media import extract_firecrawl_image_assets
from app.adapters.content.content_extractor_crawl import ContentExtractorCrawlMixin
from app.adapters.content.content_extractor_requests import ContentExtractorRequestsMixin
from app.adapters.content.platform_extraction import (
    PlatformExtractionRequest,
    PlatformExtractionRouter,
    PlatformRequestLifecycle,
)
from app.adapters.content.quality_filters import detect_low_value_content
from app.adapters.content.scraper.protocol import ContentScraperProtocol
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.application.dto.aggregation import NormalizedSourceDocument
from app.config import AppConfig
from app.core.call_status import CallStatus
from app.core.html_utils import clean_markdown_article_text, html_to_text
from app.core.lang import detect_language
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.core.url_utils import normalize_url, url_hash_sha256
from app.core.validation import safe_message_id, safe_telegram_chat_id, safe_telegram_user_id
from app.db.session import Database
from app.domain.models.source import SourceItem, SourceKind
from app.infrastructure.cache.redis_cache import RedisCache
from app.infrastructure.persistence.message_persistence import MessagePersistence
from app.observability.failure_observability import (
    REASON_FIRECRAWL_ERROR,
    REASON_FIRECRAWL_LOW_VALUE,
    persist_request_failure,
)

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.llm.protocol import LLMClientProtocol
    from app.core.telegram_progress_message import TelegramProgressMessage

logger = get_logger(__name__)

# Route versioning constants
URL_ROUTE_VERSION = 1


@dataclass
class ContentExtractionResult:
    """Structured result from extract_and_process_content."""

    request_id: int
    content_text: str
    content_source: str
    detected_lang: str
    title: str | None
    images: list[str] = field(default_factory=list)


class ContentExtractor(
    ContentExtractorRequestsMixin,
    ContentExtractorCrawlMixin,
):
    """Content extraction entry point for URL-based inputs.

    Mixin split rationale: ContentExtractorRequestsMixin owns all DB-write paths
    (request rows, crawl results, message snapshots, sender metadata) while
    ContentExtractorCrawlMixin owns scraper orchestration and HTML salvage.
    The split keeps each file focused on one responsibility and avoids a single
    1000-line class. Both mixins declare their host contract as typed class variables
    so the coupling is explicit rather than implicit duck-typing.
    """

    @property
    def firecrawl(self) -> ContentScraperProtocol:
        """Backward-compatible alias for the configured scraper chain."""
        return self.scraper

    @firecrawl.setter
    def firecrawl(self, scraper: ContentScraperProtocol) -> None:
        self.scraper = scraper

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        firecrawl: ContentScraperProtocol,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict[str, Any]], None],
        sem: Callable[[], Any],
        quality_llm_client: LLMClientProtocol | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        # Backwards-compatible alias for existing platform extractors/tests. The
        # object is normally the generic multi-provider scraper chain, not only
        # the Firecrawl client.
        self.firecrawl = firecrawl
        self.response_formatter = response_formatter
        self._audit = audit_func
        self._sem = sem
        self._quality_llm_client = quality_llm_client
        self._cache = RedisCache(cfg)
        self.message_persistence = MessagePersistence(db)
        self._platform_request_lifecycle = PlatformRequestLifecycle(
            response_formatter=response_formatter,
            message_persistence=self.message_persistence,
            audit_func=audit_func,
            route_version=URL_ROUTE_VERSION,
        )
        self._platform_router: PlatformExtractionRouter | None = None

    async def clear_cache(self) -> int:
        """Clear the extraction cache."""
        return cast(int, await self._cache.clear())

    def _aggregation_article_media_enabled(self) -> bool:
        return bool(getattr(self.cfg.runtime, "aggregation_article_media_enabled", True))

    def _get_platform_router(self) -> PlatformExtractionRouter:
        if self._platform_router is not None:
            return self._platform_router

        from app.adapters.academic.url_patterns import is_academic_paper_url
        from app.adapters.github.url_patterns import is_github_repo_url
        from app.core.urls.meta import is_instagram_url, is_threads_url
        from app.core.urls.twitter import is_twitter_url
        from app.core.urls.youtube import is_youtube_url

        router = PlatformExtractionRouter()
        # GitHub must be registered before the generic scraper chain so that
        # repo URLs short-circuit the fallback path.
        router.register(
            predicate=is_github_repo_url,
            factory=self._build_github_platform_extractor,
        )
        # Academic paper hosts (arXiv, SSRN, NBER, OSF, ResearchGate, RePEc)
        # are domain-specific and disjoint from the other platform predicates,
        # so ordering relative to youtube/twitter/meta is not load-bearing —
        # they're grouped with github here for readability.
        router.register(
            predicate=is_academic_paper_url,
            factory=self._build_academic_platform_extractor,
        )
        router.register(
            predicate=is_youtube_url,
            factory=self._build_youtube_platform_extractor,
        )
        router.register(
            predicate=lambda normalized_url: (
                bool(self.cfg.twitter.enabled) and is_twitter_url(normalized_url)
            ),
            factory=self._build_twitter_platform_extractor,
        )
        router.register(
            predicate=lambda normalized_url: (
                bool(getattr(self.cfg.runtime, "aggregation_meta_extractors_enabled", True))
                and (is_threads_url(normalized_url) or is_instagram_url(normalized_url))
            ),
            factory=self._build_meta_platform_extractor,
        )
        self._platform_router = router
        return router

    def _build_youtube_platform_extractor(self) -> Any:
        from app.adapters.youtube.platform_extractor import YouTubePlatformExtractor
        from app.infrastructure.persistence.repositories.video_download_repository import (
            VideoDownloadRepositoryAdapter,
        )

        return YouTubePlatformExtractor(
            cfg=self.cfg,
            db=self.db,
            response_formatter=self.response_formatter,
            audit_func=self._audit,
            lifecycle=self._platform_request_lifecycle,
            request_repo=self.message_persistence.request_repo,
            video_repo=VideoDownloadRepositoryAdapter(self.db),
        )

    def _build_twitter_platform_extractor(self) -> Any:
        from app.adapters.twitter.platform_extractor import TwitterPlatformExtractor

        return TwitterPlatformExtractor(
            cfg=self.cfg,
            db=self.db,
            firecrawl=self.scraper,
            response_formatter=self.response_formatter,
            message_persistence=self.message_persistence,
            firecrawl_sem=self._sem,
            schedule_crawl_persistence=self._schedule_crawl_persistence,
            lifecycle=self._platform_request_lifecycle,
        )

    def _build_meta_platform_extractor(self) -> Any:
        from app.adapters.meta.platform_extractor import MetaPlatformExtractor

        return MetaPlatformExtractor(
            cfg=self.cfg,
            scraper=self.scraper,
            firecrawl_sem=self._sem,
            lifecycle=self._platform_request_lifecycle,
        )

    def _build_academic_platform_extractor(self) -> Any:
        from app.adapters.academic.platform_extractor import AcademicPlatformExtractor

        return AcademicPlatformExtractor(
            cfg=self.cfg,
            scraper=self.scraper,
            firecrawl_sem=self._sem,
            lifecycle=self._platform_request_lifecycle,
        )

    def _build_github_platform_extractor(self) -> Any:
        from app.adapters.github.platform_extractor import GitHubPlatformExtractor
        from app.agents.repo_analysis_agent import RepoAnalysisAgent
        from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase
        from app.infrastructure.embedding.embedding_factory import create_embedding_service
        from app.infrastructure.embedding.repository_embedding import RepositoryEmbeddingGenerator

        llm_client = self._quality_llm_client
        if llm_client is None:
            raise RuntimeError(
                "GitHubPlatformExtractor requires an LLM client "
                "(quality_llm_client was not provided to ContentExtractor)"
            )

        embedding_service = create_embedding_service(self.cfg.embedding)

        try:
            from app.core.embedding_space import resolve_embedding_space_identifier
            from app.infrastructure.vector.qdrant_store import QdrantVectorStore

            qdrant_store: Any = QdrantVectorStore(
                url=self.cfg.vector_store.url,
                api_key=self.cfg.vector_store.api_key,
                environment=self.cfg.vector_store.environment,
                user_scope=self.cfg.vector_store.user_scope,
                collection_version=self.cfg.vector_store.collection_version,
                embedding_space=resolve_embedding_space_identifier(self.cfg.embedding),
                required=False,
                connection_timeout=self.cfg.vector_store.connection_timeout,
            )
            if not qdrant_store.available:
                qdrant_store = None
        except Exception:
            logger.debug("qdrant_store_unavailable_for_github", exc_info=True)
            qdrant_store = None

        embedding_gen = RepositoryEmbeddingGenerator(
            embedding_service=embedding_service,
            qdrant_store=qdrant_store,
            db=self.db,
            environment=self.cfg.vector_store.environment,
            user_scope=self.cfg.vector_store.user_scope,
        )
        agent = RepoAnalysisAgent(llm_service=llm_client)
        analyze_use_case = AnalyzeRepositoryUseCase(
            db=self.db,
            agent=agent,
            embedding_gen=embedding_gen,
        )

        return GitHubPlatformExtractor(
            db=self.db,
            github_config=self.cfg.github,
            analyze_use_case=analyze_use_case,
        )

    async def extract_content_pure(
        self,
        url: str,
        correlation_id: str | None = None,
        request_id: int | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        """Pure extraction method without message dependencies."""
        from app.core.urls.twitter import is_twitter_url
        from app.core.urls.youtube import is_youtube_url

        normalized_url = normalize_url(url)
        platform_result = await self._get_platform_router().extract(
            PlatformExtractionRequest(
                message=None,
                url_text=url,
                normalized_url=normalized_url,
                correlation_id=correlation_id,
                silent=True,
                request_id_override=request_id,
                mode="pure",
            )
        )
        if platform_result is not None:
            metadata = dict(platform_result.metadata)
            if platform_result.request_id is not None:
                metadata.setdefault("request_id", platform_result.request_id)
            metadata.setdefault("detected_lang", platform_result.detected_lang)
            if platform_result.source_item is not None:
                metadata.setdefault("source_item", platform_result.source_item.to_dict())
            if platform_result.normalized_document is not None:
                metadata.setdefault(
                    "normalized_source_document",
                    platform_result.normalized_document.model_dump(mode="json"),
                )
            return platform_result.content_text, platform_result.content_source, metadata

        logger.info(
            "pure_extraction_start",
            extra={
                "url": redact_url_for_logging(url),
                "normalized": redact_url_for_logging(normalized_url),
                "cid": correlation_id,
            },
        )

        async with self._sem():
            crawl = await self.scraper.scrape_markdown(normalized_url, request_id=request_id)

        quality_issue = detect_low_value_content(crawl)
        if quality_issue:
            reason = quality_issue["reason"]
            logger.warning(
                "pure_extraction_low_value", extra={"cid": correlation_id, "reason": reason}
            )
            if request_id is not None:
                await persist_request_failure(
                    request_repo=self.message_persistence.request_repo,
                    logger=logger,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    stage="extraction",
                    component="scraper",
                    reason_code=REASON_FIRECRAWL_LOW_VALUE,
                    error=ValueError(f"Low-value content detected: {reason}"),
                    retryable=True,
                    quality_reason=reason,
                    source_url=normalized_url,
                    content_signals=quality_issue.get("metrics")
                    if isinstance(quality_issue, dict)
                    else None,
                )
            raise ValueError(f"Low-value content detected: {reason}")

        has_markdown = bool(crawl.content_markdown and crawl.content_markdown.strip())
        has_html = bool(crawl.content_html and crawl.content_html.strip())

        if crawl.status != CallStatus.OK or not (has_markdown or has_html):
            error_msg = crawl.error_text or "Content extraction failed"
            if request_id is not None:
                await persist_request_failure(
                    request_repo=self.message_persistence.request_repo,
                    logger=logger,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    stage="extraction",
                    component="scraper",
                    reason_code=REASON_FIRECRAWL_ERROR,
                    error=ValueError(f"Extraction failed: {error_msg}"),
                    retryable=True,
                    http_status=crawl.http_status,
                    latency_ms=crawl.latency_ms,
                    source_url=normalized_url,
                    provider_error_code=crawl.response_error_code,
                )
            raise ValueError(f"Extraction failed: {error_msg}") from None

        if crawl.content_markdown and crawl.content_markdown.strip():
            content_text = clean_markdown_article_text(crawl.content_markdown)
            content_source = "markdown"
        elif crawl.content_html and crawl.content_html.strip():
            content_text = html_to_text(crawl.content_html)
            content_source = "html"
        else:
            content_text = ""
            content_source = "none"

        metadata = {
            "extraction_method": crawl.endpoint or "scraper_chain",
            "http_status": crawl.http_status,
            "endpoint": crawl.endpoint,
            "latency_ms": crawl.latency_ms,
            "content_length": len(content_text),
            "source_format": content_source,
        }
        if request_id is not None:
            metadata["request_id"] = request_id

        if crawl.metadata_json:
            metadata["firecrawl_metadata"] = crawl.metadata_json

        media_assets: list[Any] = []
        if self._aggregation_article_media_enabled():
            media_assets, media_selection = extract_firecrawl_image_assets(crawl)
            if media_selection["candidate_count"] > 0:
                metadata["media_selection"] = media_selection
        else:
            metadata["media_selection"] = {"strategy": "disabled_by_runtime_flag"}

        source_item = SourceItem.create(
            kind=SourceKind.WEB_ARTICLE,
            original_value=url,
            normalized_value=normalized_url,
            request_id=request_id,
        )
        normalized_document = NormalizedSourceDocument.from_extracted_content(
            source_item=source_item,
            text=content_text,
            title=(crawl.metadata_json or {}).get("title")
            if isinstance(crawl.metadata_json, dict)
            else None,
            detected_language=detect_language(content_text or ""),
            content_source=content_source,
            media_assets=media_assets,
            metadata=metadata,
        )
        metadata["source_item"] = source_item.to_dict()
        metadata["normalized_source_document"] = normalized_document.model_dump(mode="json")

        logger.info(
            "pure_extraction_success",
            extra={
                "cid": correlation_id,
                "content_len": len(content_text),
                "source": content_source,
            },
        )

        return content_text, content_source, metadata

    async def extract_and_process_content(
        self,
        message: Any,
        url_text: str,
        correlation_id: str | None = None,
        interaction_id: int | None = None,
        silent: bool = False,
        progress_tracker: TelegramProgressMessage | None = None,
    ) -> ContentExtractionResult:
        """Extract content from URL and return structured extraction result."""
        norm = normalize_url(url_text)
        # Extract Telegram IDs at the Telegram boundary before passing to cross-platform lifecycle
        _chat_obj = getattr(message, "chat", None) if message is not None else None
        _from_user = getattr(message, "from_user", None) if message is not None else None
        _msg_id_raw = (
            getattr(message, "id", getattr(message, "message_id", 0))
            if message is not None
            else None
        )
        platform_result = await self._get_platform_router().extract(
            PlatformExtractionRequest(
                message=message,
                url_text=url_text,
                normalized_url=norm,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                silent=silent,
                progress_tracker=progress_tracker,
                mode="interactive",
                chat_id=safe_telegram_chat_id(
                    getattr(_chat_obj, "id", None) if _chat_obj is not None else None,
                    field_name="chat_id",
                ),
                user_id=safe_telegram_user_id(
                    getattr(_from_user, "id", None) if _from_user is not None else None,
                    field_name="user_id",
                ),
                message_id=safe_message_id(_msg_id_raw, field_name="message_id"),
            )
        )
        if platform_result is not None:
            if platform_result.request_id is None:
                msg = "Interactive platform extraction requires a request_id"
                raise RuntimeError(msg)
            return ContentExtractionResult(
                request_id=platform_result.request_id,
                content_text=platform_result.content_text,
                content_source=platform_result.content_source,
                detected_lang=platform_result.detected_lang,
                title=platform_result.title,
                images=platform_result.images,
            )

        dedupe = url_hash_sha256(norm)
        logger.info(
            "url_flow_detected",
            extra={
                "url": redact_url_for_logging(url_text),
                "normalized": redact_url_for_logging(norm),
                "hash": dedupe,
                "cid": correlation_id,
            },
        )
        await self.response_formatter.send_url_accepted_notification(
            message, norm, correlation_id, silent=silent
        )
        req_id = await self._handle_request_dedupe_or_create(
            message, url_text, norm, dedupe, correlation_id
        )
        (
            content_text,
            content_source,
            title,
            images,
        ) = await self._extract_or_reuse_content_with_title(
            message, req_id, norm, dedupe, correlation_id, interaction_id, silent=silent
        )
        detected = detect_language(content_text or "")
        try:
            await self.message_persistence.request_repo.async_update_request_lang_detected(
                req_id, detected
            )
        except Exception as e:
            logger.error(
                "persist_lang_detected_error", extra={"error": str(e), "cid": correlation_id}
            )
        return ContentExtractionResult(
            request_id=req_id,
            content_text=content_text,
            content_source=content_source,
            detected_lang=detected,
            title=title,
            images=images,
        )
