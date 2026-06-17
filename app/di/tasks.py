"""Dependency constructors for Taskiq background jobs."""

from __future__ import annotations

from dataclasses import dataclass
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
    delivery_service_factory: Any
    signal_worker_factory: Any
    source_runner_factory: Any

    def create_bot_client(self) -> Any:
        return self.bot_client_factory(self.cfg)

    def create_delivery_service(self) -> Any:
        return self.delivery_service_factory(self.cfg, self.db)

    def create_signal_ingestion_worker(self) -> Any:
        return self.signal_worker_factory(self.cfg, self.db)

    def create_source_ingestion_runner(self) -> Any:
        return self.source_runner_factory(self.cfg, self.db)


@dataclass(frozen=True)
class VectorReconcileTaskRuntime:
    cfg: AppConfig
    db: Database
    embedding_generator: Any


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


def create_rss_delivery_service(cfg: AppConfig, db: Database) -> Any:
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
    return RSSDeliveryService(
        cfg=cfg.rss,
        pure_summary_service=facade,
        system_prompt_loader=lambda lang: prompt_mgr.get_system_prompt(
            lang, include_examples=True, num_examples=2
        ),
        rss_repository=RSSFeedRepositoryAdapter(db),
        scraper_chain=scraper_chain,
    )


def create_signal_ingestion_worker(cfg: AppConfig, db: Database) -> Any:
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
    return SignalIngestionWorker(
        repository=SignalSourceRepositoryAdapter(db),
        scorer=SignalScoringService(
            topic_similarity=VectorTopicSimilarityAdapter(
                vector_store=vector_store,
                embedding_service=embedding_service,
            )
        ),
    )


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


def build_vector_reconcile_task_runtime(
    cfg: AppConfig,
    db: Database,
) -> VectorReconcileTaskRuntime:
    from app.application.services.summary_embedding_generator import SummaryEmbeddingGenerator
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
    return VectorReconcileTaskRuntime(
        cfg=cfg,
        db=db,
        embedding_generator=SummaryEmbeddingGenerator(
            embedding_repository=EmbeddingRepositoryAdapter(db),
            request_repository=RequestRepositoryAdapter(db),
            summary_repository=SummaryRepositoryAdapter(db),
            embedding_service=embedding_service,
            max_token_length=cfg.embedding.max_token_length,
        ),
    )
