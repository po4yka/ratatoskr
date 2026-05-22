"""Worker-process dependency providers for TaskiqDepends.

Factories are module-level singletons (lru_cache) so each worker process
opens the DB and loads config once.  Factory helper functions that produce
fresh service objects on every task run are plain callables — not cached —
because each run needs a fresh Telethon/OpenRouter client lifecycle.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from tempfile import gettempdir
from typing import Any, cast

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime

# ── singleton providers ───────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _cached_config() -> AppConfig:
    from app.config import load_config

    return load_config()


async def get_app_config() -> AppConfig:
    """Return the cached AppConfig singleton for this worker process."""
    return _cached_config()


_db_instance: Database | None = None


async def get_db(cfg: AppConfig = TaskiqDepends(get_app_config)) -> Database:
    """Return a cached Database facade for this worker process."""
    global _db_instance
    if _db_instance is None:
        from app.db.session import Database

        _db_instance = Database(config=cfg.database)
    return _db_instance


# ── digest factory helpers ────────────────────────────────────────────────────


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


# ── RSS / signal factory helpers ──────────────────────────────────────────────


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
    from app.adapters.content.pure_summary_service import PureSummaryService
    from app.adapters.content.summarization_runtime import SummarizationRuntime
    from app.adapters.openrouter.openrouter_client import OpenRouterClient
    from app.adapters.rss.rss_delivery_service import RSSDeliveryService
    from app.di.repositories import (
        build_crawl_result_repository,
        build_llm_repository,
        build_request_repository,
        build_summary_repository,
        build_user_repository,
    )
    from app.di.shared import (
        LazySemaphoreFactory,
        build_response_formatter,
        build_scraper_chain,
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
    runtime = SummarizationRuntime(
        cfg=cfg,
        db=db,
        openrouter=llm_client,
        response_formatter=response_formatter,
        audit_func=lambda *_a, **_kw: None,
        sem=sem_factory,
        summary_repo=build_summary_repository(db),
        request_repo=build_request_repository(db),
        crawl_result_repo=build_crawl_result_repository(db),
        llm_repo=build_llm_repository(db),
        user_repo=build_user_repository(db),
    )
    pure_service = PureSummaryService(runtime=runtime)
    prompt_mgr = get_prompt_manager()
    scraper_chain = None
    if cfg.rss.scrape_short_content:
        scraper_chain = build_scraper_chain(cfg, audit=lambda *_a, **_kw: None)
    return RSSDeliveryService(
        cfg=cfg.rss,
        pure_summary_service=pure_service,
        system_prompt_loader=lambda lang: prompt_mgr.get_system_prompt(
            lang, include_examples=True, num_examples=2
        ),
        rss_repository=RSSFeedRepositoryAdapter(db),
        scraper_chain=scraper_chain,  # type: ignore[arg-type]
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
    from app.infrastructure.persistence.repositories.signal_source_repository import (
        SignalSourceRepositoryAdapter,
    )

    return SourceIngestionRunner(
        repository=SignalSourceRepositoryAdapter(db),
        ingesters=create_source_ingesters(cfg.signal_ingestion),
        subscriber_user_ids=cfg.telegram.allowed_user_ids,
    )
