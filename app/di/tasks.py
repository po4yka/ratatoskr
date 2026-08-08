"""Dependency constructors for Taskiq background jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.db.session import Database


@dataclass(frozen=True)
class DigestTaskRuntime:
    cfg: AppConfig
    userbot_factory: Any
    llm_client_factory: Any
    bot_client_factory: Any
    service_factory: Any

    def create_userbot(self) -> Any:
        return self.userbot_factory(self.cfg)

    def create_llm_client(self) -> Any:
        return self.llm_client_factory(self.cfg)

    def create_bot_client(self) -> Any:
        return self.bot_client_factory(self.cfg)

    def create_service(self, *, userbot: Any, llm_client: Any, send_message: Any) -> Any:
        return self.service_factory(
            self.cfg,
            userbot=userbot,
            llm_client=llm_client,
            send_message=send_message,
        )


@dataclass(frozen=True)
class RssPollTaskRuntime:
    cfg: AppConfig
    db: Database
    bot_client_factory: Any
    signal_worker_factory: Any
    source_runner_factory: Any
    delivery_service_factory: Any
    # Everything the factories built for this run. The poll task closes these
    # when it finishes: each cycle used to construct a fresh OpenRouterClient,
    # scraper chain and Qdrant store and abandon them, accumulating HTTP clients
    # on a worker that lives for days.
    closables: list[Any] = field(default_factory=list)

    def create_bot_client(self) -> Any:
        return self.bot_client_factory(self.cfg)

    def create_delivery_service(self) -> Any:
        service, closables = self.delivery_service_factory(self.cfg, self.db)
        self.closables.extend(closables)
        return service

    def create_signal_ingestion_worker(self) -> Any:
        worker, closables = self.signal_worker_factory(self.cfg, self.db)
        self.closables.extend(closables)
        return worker

    def create_source_ingestion_runner(self) -> Any:
        return self.source_runner_factory(self.cfg, self.db)

    async def aclose(self) -> None:
        """Release everything the factories built for this poll."""
        from app.di.shared import close_runtime_resources

        pending, self.closables[:] = list(self.closables), []
        await close_runtime_resources(*pending)


@dataclass(frozen=True)
class VectorReconcileTaskRuntime:
    cfg: AppConfig
    db: Database
    embedding_generator: Any
    embedding_repository: Any
    vector_store: Any


@dataclass(frozen=True)
class SourceContentReconcileTaskRuntime:
    cfg: AppConfig
    db: Database
    service: Any


@dataclass(frozen=True)
class XBookmarksTaskRuntime:
    cfg: AppConfig
    db: Database
    ingestor: Any


@dataclass(frozen=True)
class XWikiSyncTaskRuntime:
    cfg: AppConfig
    db: Database
    service: Any


@dataclass(frozen=True)
class GitBackupTaskRuntime:
    cfg: AppConfig
    db: Database
    service: Any  # GitMirrorService


@dataclass(frozen=True)
class StarListFilingTaskRuntime:
    cfg: AppConfig
    db: Database
    suggester: Any  # SuggestStarListsUseCase | None -- None without a vector store
    star_lists: Any  # ManageStarListsUseCase


def create_digest_userbot(cfg: AppConfig) -> Any:
    from app.adapters.digest.userbot_client import UserbotClient

    return UserbotClient(cfg, Path("/data"))


def create_digest_llm_client(cfg: AppConfig) -> Any:
    from app.adapters.openrouter.openrouter_client import OpenRouterClient

    return OpenRouterClient(
        api_key=cfg.openrouter.api_key,
        model=cfg.openrouter.model,
        fallback_models=cfg.openrouter.fallback_models,
    )


def create_digest_bot_client(cfg: AppConfig) -> Any:
    from app.adapters.telegram.telethon_compat import TelethonBotClient

    return TelethonBotClient(
        name="digest_bot_sender",
        api_id=cfg.telegram.api_id,
        api_hash=cfg.telegram.api_hash,
        bot_token=cfg.telegram.bot_token,
        session_dir=gettempdir(),
    )


def create_digest_service(
    cfg: AppConfig,
    *,
    userbot: Any,
    llm_client: Any,
    send_message: Any,
) -> Any:
    from app.adapters.digest.analyzer import DigestAnalyzer
    from app.adapters.digest.channel_reader import ChannelReader
    from app.adapters.digest.digest_service import DigestService
    from app.adapters.digest.formatter import DigestFormatter

    reader = ChannelReader(cfg, userbot)
    analyzer = DigestAnalyzer(cfg, llm_client)
    formatter = DigestFormatter()
    return DigestService(
        cfg=cfg,
        reader=reader,
        analyzer=analyzer,
        formatter=formatter,
        send_message_func=send_message,
    )


def build_digest_task_runtime(cfg: AppConfig) -> DigestTaskRuntime:
    return DigestTaskRuntime(
        cfg=cfg,
        userbot_factory=create_digest_userbot,
        llm_client_factory=create_digest_llm_client,
        bot_client_factory=create_digest_bot_client,
        service_factory=create_digest_service,
    )


def create_rss_bot_client(cfg: AppConfig) -> Any:
    from app.adapters.telegram.telethon_compat import TelethonBotClient

    return TelethonBotClient(
        name="rss_bot_sender",
        api_id=cfg.telegram.api_id,
        api_hash=cfg.telegram.api_hash,
        bot_token=cfg.telegram.bot_token,
        session_dir=gettempdir(),
    )


def create_rss_delivery_service(cfg: AppConfig, db: Database) -> tuple[Any, list[Any]]:
    """Build the RSS delivery service and hand back what the caller must close.

    The clients live only as long as one poll, so the caller owns their
    shutdown; returning them explicitly beats digging them back out of the wired
    service, and beats leaving them to be collected (an abandoned httpx client
    does not close its connections on garbage collection).
    """
    from app.adapters.openrouter.openrouter_client import OpenRouterClient
    from app.adapters.rss.rss_delivery_service import RSSDeliveryService
    from app.di.shared import (
        LazySemaphoreFactory,
        build_response_formatter,
        build_scraper_chain,
        build_url_processor,
    )
    from app.infrastructure.persistence.repositories.rss_feed_repository import (
        RSSFeedRepositoryAdapter,
    )
    from app.prompts.manager import get_prompt_manager

    llm_client = OpenRouterClient(
        api_key=cfg.openrouter.api_key,
        model=cfg.openrouter.model,
        fallback_models=cfg.openrouter.fallback_models,
    )
    response_formatter = cast("Any", build_response_formatter(cfg))
    sem_factory = LazySemaphoreFactory(cfg.runtime.max_concurrent_calls)
    # T9 cutover: the graph is the only summarize path. RSS uses the content-only
    # ``facade.summarize`` (byte-identical signature to the legacy
    # ``PureSummaryService.summarize`` the service consumed). The legacy URLProcessor
    # inside the facade is a collaborator bag; RSS never drives ``handle_url_flow``.
    facade = build_url_processor(
        cfg=cfg,
        db=db,
        firecrawl=cast("Any", build_scraper_chain(cfg, audit=lambda *_a, **_kw: None)),
        openrouter=llm_client,
        response_formatter=response_formatter,
        audit_func=lambda *_a, **_kw: None,
        sem=sem_factory,
    )
    prompt_mgr = get_prompt_manager()
    scraper_chain = None
    if cfg.rss.scrape_short_content:
        scraper_chain = cast("Any", build_scraper_chain(cfg, audit=lambda *_a, **_kw: None))
    service = RSSDeliveryService(
        cfg=cfg.rss,
        pure_summary_service=facade,
        system_prompt_loader=lambda lang: prompt_mgr.get_system_prompt(
            lang, include_examples=True, num_examples=2
        ),
        rss_repository=RSSFeedRepositoryAdapter(db),
        scraper_chain=scraper_chain,
    )
    return service, [facade, llm_client, scraper_chain]


def create_signal_ingestion_worker(cfg: AppConfig, db: Database) -> tuple[Any, list[Any]]:
    """Build the signal-ingestion worker and hand back what the caller must close."""
    from app.application.services.signal_ingestion_worker import SignalIngestionWorker
    from app.application.services.signal_scoring import SignalScoringService
    from app.di.shared import build_qdrant_vector_store
    from app.infrastructure.embedding.embedding_factory import create_embedding_service
    from app.infrastructure.persistence.repositories.signal_source_repository import (
        SignalSourceRepositoryAdapter,
    )
    from app.infrastructure.search.vector_topic_similarity import VectorTopicSimilarityAdapter

    embedding_service = create_embedding_service(cfg.embedding)
    vector_store = build_qdrant_vector_store(cfg)
    worker = SignalIngestionWorker(
        repository=SignalSourceRepositoryAdapter(db),
        scorer=SignalScoringService(
            topic_similarity=VectorTopicSimilarityAdapter(
                vector_store=vector_store,
                embedding_service=embedding_service,
            )
        ),
    )
    return worker, [vector_store, embedding_service]


def create_source_ingestion_runner(cfg: AppConfig, db: Database) -> Any:
    from app.adapters.ingestors.registry import create_source_ingesters
    from app.adapters.ingestors.runner import SourceIngestionRunner
    from app.application.ports.source_ingestors import SourceIngesterBuildContext
    from app.di.repositories import build_social_connection_repository
    from app.di.social import build_social_token_resolver
    from app.infrastructure.persistence.repositories.signal_source_repository import (
        SignalSourceRepositoryAdapter,
    )

    subscriber_user_ids = tuple(int(user_id) for user_id in cfg.telegram.allowed_user_ids)
    social_connection_repository = build_social_connection_repository(db)
    social_token_resolver = build_social_token_resolver(cfg, social_connection_repository)
    return SourceIngestionRunner(
        repository=SignalSourceRepositoryAdapter(db),
        ingesters=create_source_ingesters(
            cfg.signal_ingestion,
            context=SourceIngesterBuildContext(
                social_connection_repository=social_connection_repository,
                social_token_resolver=social_token_resolver,
                subscriber_user_ids=subscriber_user_ids,
                x_api_base_url=cfg.twitter.x_api_base_url,
                threads_graph_base_url=cfg.social.threads_graph_base_url,
            ),
        ),
        subscriber_user_ids=subscriber_user_ids,
    )


def build_rss_poll_task_runtime(cfg: AppConfig, db: Database) -> RssPollTaskRuntime:
    return RssPollTaskRuntime(
        cfg=cfg,
        db=db,
        bot_client_factory=create_rss_bot_client,
        delivery_service_factory=create_rss_delivery_service,
        signal_worker_factory=create_signal_ingestion_worker,
        source_runner_factory=create_source_ingestion_runner,
    )


def build_x_bookmarks_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> XBookmarksTaskRuntime:
    from app.adapters.ingestors.x_bookmarks_ingestor import XBookmarksIngestor

    return XBookmarksTaskRuntime(
        cfg=cfg,
        db=db,
        ingestor=XBookmarksIngestor(
            database=db,
            bookmarks_db_path=cfg.x_bookmarks.bookmarks_db_path,
        ),
    )


def build_x_wiki_sync_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> XWikiSyncTaskRuntime:
    from app.application.services.x_wiki_sync import XWikiSyncService
    from app.di.shared import build_qdrant_vector_store
    from app.infrastructure.embedding.embedding_factory import create_embedding_service

    return XWikiSyncTaskRuntime(
        cfg=cfg,
        db=db,
        service=XWikiSyncService(
            library_path=cfg.x_bookmarks.library_path,
            vector_store=build_qdrant_vector_store(cfg),
            embedding_service=create_embedding_service(cfg.embedding),
        ),
    )


def build_git_backup_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> GitBackupTaskRuntime:
    from app.adapters.git_backup.mirror_service import GitMirrorService
    from app.adapters.git_backup.repository import GitMirrorRepository

    mirror_repo = GitMirrorRepository(db=db, config=cfg.git_backup)
    service = GitMirrorService(
        config=cfg.git_backup,
        mirror_repo=mirror_repo,
        db=db,
    )
    return GitBackupTaskRuntime(cfg=cfg, db=db, service=service)


def build_star_list_filing_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> StarListFilingTaskRuntime:
    """Compose the scheduled star-list filing pass.

    The suggester comes from the same factory the API uses, so a repository filed
    by this job lands where it would have landed had the user added it through
    ``POST /v1/repositories`` -- the two paths must not disagree about the same
    repository just because one of them ran at night.
    """
    from app.adapters.github.github_graphql_client import GitHubGraphQLClient
    from app.application.use_cases.manage_star_lists import ManageStarListsUseCase
    from app.di.repositories import build_llm_repository
    from app.di.shared import build_core_dependencies, build_qdrant_vector_store
    from app.di.star_lists import build_star_list_suggester
    from app.infrastructure.embedding.embedding_factory import create_embedding_service
    from app.infrastructure.persistence.repositories.github_integration_repository import (
        GitHubIntegrationRepository,
    )
    from app.infrastructure.persistence.repositories.repository_read_repository import (
        RepositoryReadRepositoryAdapter,
    )

    core = build_core_dependencies(cfg, db)
    suggester = build_star_list_suggester(
        app_cfg=cfg,
        database=db,
        embedding_service=create_embedding_service(cfg.embedding),
        vector_store=build_qdrant_vector_store(cfg),
        llm_client=core.llm_client,
        llm_repository=build_llm_repository(db),
    )
    star_lists = ManageStarListsUseCase(
        gateway_factory=GitHubGraphQLClient,
        repository_repo=RepositoryReadRepositoryAdapter(db),
        integration_repo=GitHubIntegrationRepository(db),
    )
    return StarListFilingTaskRuntime(
        cfg=cfg,
        db=db,
        suggester=suggester,
        star_lists=star_lists,
    )


def build_vector_reconcile_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> VectorReconcileTaskRuntime:
    from app.application.services.summary_embedding_generator import SummaryEmbeddingGenerator
    from app.di.shared import build_qdrant_vector_store
    from app.infrastructure.embedding.embedding_factory import create_embedding_service
    from app.infrastructure.persistence.repositories.embedding_repository import (
        EmbeddingRepositoryAdapter,
    )
    from app.infrastructure.persistence.repositories.request_repository import (
        RequestRepositoryAdapter,
    )
    from app.infrastructure.persistence.repositories.summary_repository import (
        SummaryRepositoryAdapter,
    )

    embedding_service = create_embedding_service(cfg.embedding)
    embedding_repository = EmbeddingRepositoryAdapter(db)
    return VectorReconcileTaskRuntime(
        cfg=cfg,
        db=db,
        embedding_repository=embedding_repository,
        vector_store=build_qdrant_vector_store(cfg),
        embedding_generator=SummaryEmbeddingGenerator(
            embedding_repository=embedding_repository,
            request_repository=RequestRepositoryAdapter(db),
            summary_repository=SummaryRepositoryAdapter(db),
            embedding_service=embedding_service,
            max_token_length=cfg.embedding.max_token_length,
        ),
    )


def build_source_content_reconcile_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> SourceContentReconcileTaskRuntime:
    from app.adapters.content.content_extractor import ContentExtractor
    from app.application.services.source_content_backfill_service import (
        SourceContentBackfillService,
    )
    from app.application.use_cases.summary_read_model import SummaryReadModelUseCase
    from app.di.platform_extractors import build_registered_platform_router
    from app.di.shared import build_core_dependencies
    from app.infrastructure.persistence.repositories.crawl_result_repository import (
        CrawlResultRepositoryAdapter,
    )
    from app.infrastructure.persistence.repositories.llm_repository import (
        LLMRepositoryAdapter,
    )
    from app.infrastructure.persistence.repositories.request_repository import (
        RequestRepositoryAdapter,
    )
    from app.infrastructure.persistence.repositories.summary_repository import (
        SummaryRepositoryAdapter,
    )

    core = build_core_dependencies(cfg, db)
    source_extractor = ContentExtractor(
        cfg=cfg,
        db=db,
        firecrawl=core.scraper_chain,
        response_formatter=core.response_formatter,
        audit_func=core.audit_sink,
        sem=core.semaphore_factory,
        quality_llm_client=core.llm_client,
        platform_router=build_registered_platform_router(
            cfg=cfg,
            db=db,
            scraper=core.scraper_chain,
            response_formatter=core.response_formatter,
            audit_func=core.audit_sink,
            sem=core.semaphore_factory,
            quality_llm_client=core.llm_client,
        ),
    )
    summary_repository = SummaryRepositoryAdapter(db)
    request_repository = RequestRepositoryAdapter(db)
    crawl_repository = CrawlResultRepositoryAdapter(db)
    summary_reader = SummaryReadModelUseCase(
        summary_repository,
        request_repository,
        crawl_repository,
        LLMRepositoryAdapter(db),
    )
    return SourceContentReconcileTaskRuntime(
        cfg=cfg,
        db=db,
        service=SourceContentBackfillService(
            summary_reader=summary_reader,
            source_extractor=source_extractor,
            source_writer=crawl_repository,
            persist_source_content=cfg.retention.persist_reader_source_content,
        ),
    )
