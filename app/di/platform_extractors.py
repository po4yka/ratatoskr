"""DI-owned platform extractor contributions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.content.content_extractor import URL_ROUTE_VERSION
from app.adapters.content.content_extractor_requests import schedule_crawl_persistence_task
from app.adapters.content.platform_extraction import (
    PlatformExtractionRouter,
    PlatformExtractorContext,
    PlatformExtractorContribution,
    PlatformRequestLifecycle,
    build_platform_extraction_router,
)
from app.core.logging_utils import get_logger
from app.infrastructure.persistence.message_persistence import MessagePersistence

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from app.adapters.content.scraper.protocol import ContentScraperProtocol
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.llm.protocol import LLMClientProtocol
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)


def build_registered_platform_router(
    *,
    cfg: AppConfig,
    db: Database,
    scraper: ContentScraperProtocol,
    response_formatter: ResponseFormatter,
    audit_func: Callable[[str, str, dict[str, Any]], None],
    sem: Callable[[], Any],
    quality_llm_client: LLMClientProtocol | None,
) -> PlatformExtractionRouter:
    """Build the registered platform extraction router for runtime use."""
    message_persistence = MessagePersistence(db)
    lifecycle = PlatformRequestLifecycle(
        response_formatter=response_formatter,
        message_persistence=message_persistence,
        audit_func=audit_func,
        route_version=URL_ROUTE_VERSION,
    )
    context = PlatformExtractorContext(
        cfg=cfg,
        db=db,
        scraper=scraper,
        response_formatter=response_formatter,
        audit_func=audit_func,
        sem=sem,
        message_persistence=message_persistence,
        lifecycle=lifecycle,
        quality_llm_client=quality_llm_client,
        schedule_crawl_persistence=lambda req_id, crawl, correlation_id: (
            schedule_crawl_persistence_task(
                cfg=cfg,
                message_persistence=message_persistence,
                req_id=req_id,
                crawl=crawl,
                correlation_id=correlation_id,
            )
        ),
    )
    return build_platform_extraction_router(
        build_platform_extractor_contributions(cfg),
        context,
    )


def build_platform_extractor_contributions(
    cfg: AppConfig,
) -> Sequence[PlatformExtractorContribution]:
    """Return built-in platform extractor contributions in routing order."""
    from app.adapters.academic.url_patterns import is_academic_paper_url
    from app.adapters.github.url_patterns import is_github_repo_url
    from app.core.urls.meta import is_instagram_url, is_threads_url
    from app.core.urls.twitter import is_twitter_url
    from app.core.urls.youtube import is_youtube_url

    return (
        PlatformExtractorContribution(
            name="github",
            predicate=is_github_repo_url,
            factory=_build_github_platform_extractor,
        ),
        PlatformExtractorContribution(
            name="academic",
            predicate=is_academic_paper_url,
            factory=_build_academic_platform_extractor,
        ),
        PlatformExtractorContribution(
            name="youtube",
            predicate=is_youtube_url,
            factory=_build_youtube_platform_extractor,
        ),
        PlatformExtractorContribution(
            name="twitter",
            predicate=lambda normalized_url: (
                bool(cfg.twitter.enabled) and is_twitter_url(normalized_url)
            ),
            factory=_build_twitter_platform_extractor,
        ),
        PlatformExtractorContribution(
            name="meta",
            predicate=lambda normalized_url: (
                bool(getattr(cfg.runtime, "aggregation_meta_extractors_enabled", True))
                and (is_threads_url(normalized_url) or is_instagram_url(normalized_url))
            ),
            factory=_build_meta_platform_extractor,
        ),
    )


def _build_youtube_platform_extractor(context: PlatformExtractorContext) -> Any:
    from app.adapters.transcription import get_or_create_transcription_service
    from app.adapters.youtube.platform_extractor import YouTubePlatformExtractor
    from app.infrastructure.persistence.repositories.video_download_repository import (
        VideoDownloadRepositoryAdapter,
    )

    transcription_service = None
    transcription_cfg = getattr(context.cfg, "transcription", None)
    if transcription_cfg is not None and bool(getattr(transcription_cfg, "enabled", False)):
        transcription_service = get_or_create_transcription_service(transcription_cfg)

    return YouTubePlatformExtractor(
        cfg=context.cfg,
        db=context.db,
        response_formatter=context.response_formatter,
        audit_func=context.audit_func,
        lifecycle=context.lifecycle,
        request_repo=context.message_persistence.request_repo,
        video_repo=VideoDownloadRepositoryAdapter(context.db),
        transcription_service=transcription_service,
    )


def _build_twitter_platform_extractor(context: PlatformExtractorContext) -> Any:
    from app.adapters.twitter.platform_extractor import TwitterPlatformExtractor

    return TwitterPlatformExtractor(
        cfg=context.cfg,
        db=context.db,
        firecrawl=context.scraper,
        response_formatter=context.response_formatter,
        message_persistence=context.message_persistence,
        firecrawl_sem=context.sem,
        schedule_crawl_persistence=context.schedule_crawl_persistence,
        lifecycle=context.lifecycle,
    )


def _build_meta_platform_extractor(context: PlatformExtractorContext) -> Any:
    from app.adapters.meta.instagram_api_extractor import InstagramApiExtractor
    from app.adapters.meta.platform_extractor import MetaPlatformExtractor
    from app.adapters.meta.threads_api_extractor import ThreadsApiExtractor
    from app.adapters.social.meta import (
        InstagramClient,
        InstagramOAuthConfig,
        ThreadsClient,
        ThreadsOAuthConfig,
    )
    from app.application.services.social_token_service import SocialAccessTokenResolver
    from app.infrastructure.persistence.repositories.social_connection_repository import (
        SocialConnectionRepositoryAdapter,
    )

    social_cfg = context.cfg.social
    social_repository = SocialConnectionRepositoryAdapter(context.db)
    threads_client = ThreadsClient(
        ThreadsOAuthConfig(
            client_id=social_cfg.threads_client_id,
            client_secret=social_cfg.threads_client_secret.get_secret_value()
            if social_cfg.threads_client_secret is not None
            else None,
            redirect_uri=social_cfg.threads_redirect_uri,
            scopes=social_cfg.threads_scopes,
            graph_base_url=social_cfg.threads_graph_base_url,
        )
    )
    instagram_client = InstagramClient(
        InstagramOAuthConfig(
            client_id=social_cfg.instagram_client_id,
            client_secret=social_cfg.instagram_client_secret.get_secret_value()
            if social_cfg.instagram_client_secret is not None
            else None,
            redirect_uri=social_cfg.instagram_redirect_uri,
            scopes=social_cfg.instagram_scopes,
            graph_base_url=social_cfg.instagram_graph_base_url,
        )
    )
    token_resolver = SocialAccessTokenResolver(
        repository=social_repository,
        oauth_clients={"threads": threads_client, "instagram": instagram_client},
    )

    return MetaPlatformExtractor(
        cfg=context.cfg,
        scraper=context.scraper,
        firecrawl_sem=context.sem,
        lifecycle=context.lifecycle,
        threads_api_extractor=ThreadsApiExtractor(
            repository=social_repository,
            threads_client=threads_client,
            token_resolver=token_resolver,
        ),
        instagram_api_extractor=InstagramApiExtractor(
            repository=social_repository,
            instagram_client=instagram_client,
            token_resolver=token_resolver,
        ),
    )


def _build_academic_platform_extractor(context: PlatformExtractorContext) -> Any:
    from app.adapters.academic.platform_extractor import AcademicPlatformExtractor

    return AcademicPlatformExtractor(
        cfg=context.cfg,
        scraper=context.scraper,
        firecrawl_sem=context.sem,
        lifecycle=context.lifecycle,
    )


def _build_github_platform_extractor(context: PlatformExtractorContext) -> Any:
    from app.adapters.github.platform_extractor import GitHubPlatformExtractor
    from app.agents.repo_analysis_agent import RepoAnalysisAgent
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase
    from app.infrastructure.embedding.embedding_factory import create_embedding_service
    from app.infrastructure.embedding.repository_embedding import RepositoryEmbeddingGenerator
    from app.infrastructure.persistence.repositories.repository_analysis_repository import (
        RepositoryAnalysisRepositoryAdapter,
    )

    llm_client = context.quality_llm_client
    if llm_client is None:
        raise RuntimeError(
            "GitHubPlatformExtractor requires an LLM client "
            "(quality_llm_client was not provided to ContentExtractor)"
        )

    embedding_service = create_embedding_service(context.cfg.embedding)

    try:
        from app.core.embedding_space import resolve_embedding_space_identifier
        from app.infrastructure.vector.qdrant_store import QdrantVectorStore

        qdrant_store: Any = QdrantVectorStore(
            url=context.cfg.vector_store.url,
            api_key=context.cfg.vector_store.api_key,
            environment=context.cfg.vector_store.environment,
            user_scope=context.cfg.vector_store.user_scope,
            collection_version=context.cfg.vector_store.collection_version,
            embedding_space=resolve_embedding_space_identifier(context.cfg.embedding),
            required=False,
            connection_timeout=context.cfg.vector_store.connection_timeout,
        )
        if not qdrant_store.available:
            qdrant_store = None
    except Exception:
        logger.debug("qdrant_store_unavailable_for_github", exc_info=True)
        qdrant_store = None

    embedding_gen = RepositoryEmbeddingGenerator(
        embedding_service=embedding_service,
        qdrant_store=qdrant_store,
        db=context.db,
        environment=context.cfg.vector_store.environment,
        user_scope=context.cfg.vector_store.user_scope,
    )
    agent = RepoAnalysisAgent(llm_service=llm_client)
    repository_repo = RepositoryAnalysisRepositoryAdapter(context.db)
    analyze_use_case = AnalyzeRepositoryUseCase(
        repository_repo=repository_repo,
        agent=agent,
        embedding_gen=embedding_gen,
    )

    return GitHubPlatformExtractor(
        db=context.db,
        github_config=context.cfg.github,
        analyze_use_case=analyze_use_case,
    )
