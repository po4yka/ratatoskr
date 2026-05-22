from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.adapters.content.scraper.factory import ContentScraperFactory
from app.adapters.external.firecrawl.client import FirecrawlClient
from app.adapters.external.response_formatter import ResponseFormatter
from app.adapters.llm import LLMClientFactory
from app.core.logging_utils import get_logger
from app.di.repositories import (
    build_audit_log_repository,
    build_crawl_result_repository,
    build_llm_repository,
    build_request_repository,
    build_summary_repository,
    build_user_repository,
)
from app.di.platform_extractors import build_registered_platform_router
from app.di.types import CoreDependencies

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.content.scraper.protocol import ContentScraperProtocol
    from app.application.ports.requests import RequestRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.services.related_reads_service import RelatedReadsService
    from app.application.services.topic_search import TopicSearchService
    from app.config import AppConfig
    from app.db.session import Database
    from app.db.write_queue import DbWriteQueue

logger = get_logger(__name__)


class LazySemaphoreFactory:
    """Lazy semaphore factory mirroring runtime bot behavior."""

    def __init__(self, permits: int) -> None:
        self._permits = max(1, permits)
        self._semaphore: asyncio.Semaphore = asyncio.Semaphore(self._permits)

    def __call__(self) -> asyncio.Semaphore:
        return self._semaphore


def build_async_audit_sink(
    db: Database,
    *,
    task_registry: set[asyncio.Task[Any]] | None = None,
) -> Callable[[str, str, dict[str, Any]], None]:
    """Create an async fire-and-forget audit callback backed by the DB."""
    repo = build_audit_log_repository(db)

    def audit(level: str, event: str, details: dict[str, Any]) -> None:
        payload = details if isinstance(details, dict) else {"details": str(details)}

        async def _write() -> None:
            try:
                await repo.async_insert_audit_log(
                    log_level=level,
                    event_type=event,
                    details=payload,
                )
            except Exception as exc:
                logger.warning(
                    "audit_persist_failed",
                    extra={"event": event, "error": str(exc)},
                )

        try:
            task = asyncio.create_task(_write())
        except RuntimeError as exc:
            logger.debug("audit_task_schedule_skipped", extra={"error": str(exc)})
            return

        if task_registry is not None:
            task_registry.add(task)
            task.add_done_callback(task_registry.discard)

    return audit


def resolve_ui_lang(cfg: AppConfig) -> str:
    ui_lang = cfg.runtime.preferred_lang
    return "en" if ui_lang == "auto" else ui_lang


def build_scraper_chain(
    cfg: AppConfig,
    *,
    audit: Callable[[str, str, dict[str, Any]], None] | None = None,
) -> ContentScraperProtocol:
    """Construct the content scraper chain.

    Centralised here so the architecture lint
    (test_runtime_resource_construction_is_centralized_in_app_di) holds:
    only `app/di/**`, `app/cli/**`, and `app/bootstrap/**` are allowed
    to call ContentScraperFactory.create_from_config directly.
    """
    audit_func = audit or _default_audit
    return ContentScraperFactory.create_from_config(cfg, audit=audit_func)


def build_qdrant_vector_store(cfg: AppConfig) -> Any:
    """Construct a QdrantVectorStore from configuration.

    Centralised so callers outside `app/di/**`, `app/cli/**`, and
    `app/bootstrap/**` (notably `app/tasks/deps.py`) do not need to
    instantiate the store directly. The lazy import keeps the
    `qdrant-client` dependency optional for callers that don't need it.
    """
    from app.core.embedding_space import resolve_embedding_space_identifier
    from app.infrastructure.vector.qdrant_store import QdrantVectorStore

    return QdrantVectorStore(
        url=cfg.vector_store.url,
        api_key=cfg.vector_store.api_key,
        environment=cfg.vector_store.environment,
        user_scope=cfg.vector_store.user_scope,
        collection_version=cfg.vector_store.collection_version,
        embedding_space=resolve_embedding_space_identifier(cfg.embedding),
        required=cfg.vector_store.required,
        connection_timeout=cfg.vector_store.connection_timeout,
    )


def build_response_formatter(cfg: AppConfig, **overrides: Any) -> ResponseFormatter:
    """Construct the Telegram-aware ResponseFormatter.

    Centralised here so callers outside `app/di/**`, `app/cli/**`, and
    `app/bootstrap/**` do not need to instantiate ResponseFormatter
    directly (architecture lint enforces that).
    """
    kwargs: dict[str, Any] = {
        "telegram_limits": cfg.telegram_limits,
        "telegram_config": cfg.telegram,
        "lang": resolve_ui_lang(cfg),
    }
    kwargs.update(overrides)
    return ResponseFormatter(**kwargs)


def build_core_dependencies(
    cfg: AppConfig,
    db: Database,
    *,
    audit_sink: Callable[[str, str, dict[str, Any]], None] | None = None,
    semaphore_factory: Callable[[], asyncio.Semaphore] | None = None,
    response_formatter_kwargs: dict[str, Any] | None = None,
) -> CoreDependencies:
    """Build the shared LLM, scraper, formatter, and concurrency resources."""
    audit = audit_sink or _default_audit
    sem_factory = semaphore_factory or LazySemaphoreFactory(cfg.runtime.max_concurrent_calls)
    firecrawl_client = _build_firecrawl_client(cfg, audit)
    llm_client = LLMClientFactory.create_from_config(cfg, audit=audit)
    scraper_chain = ContentScraperFactory.create_from_config(cfg, audit=audit)

    response_kwargs = dict(response_formatter_kwargs or {})
    response_formatter = ResponseFormatter(
        telegram_limits=cfg.telegram_limits,
        telegram_config=cfg.telegram,
        lang=resolve_ui_lang(cfg),
        **response_kwargs,
    )

    return CoreDependencies(
        cfg=cfg,
        db=db,
        audit_sink=audit,
        semaphore_factory=sem_factory,
        llm_client=llm_client,
        scraper_chain=scraper_chain,
        response_formatter=response_formatter,
        firecrawl_client=firecrawl_client,
    )


def build_url_processor(
    *,
    cfg: AppConfig,
    db: Database,
    firecrawl: ContentScraperProtocol,
    openrouter: Any,
    response_formatter: Any,
    audit_func: Callable[[str, str, dict[str, Any]], None],
    sem: Callable[[], asyncio.Semaphore],
    topic_search: TopicSearchService | None = None,
    db_write_queue: DbWriteQueue | None = None,
    request_repo: RequestRepositoryPort | None = None,
    summary_repo: SummaryRepositoryPort | None = None,
    crawl_result_repo: Any | None = None,
    llm_repo: Any | None = None,
    user_repo: Any | None = None,
    related_reads_service: RelatedReadsService | None = None,
) -> Any:
    """Build the shared URL processor graph for Telegram, API, and CLI runtimes."""
    from app.adapters.content.url_processor import URLProcessor
    from app.adapters.telegram.summary_draft_streaming import SummaryDraftStreamCoordinator

    request_repository = request_repo or build_request_repository(db)
    summary_repository = summary_repo or build_summary_repository(db)
    crawl_repository = crawl_result_repo or build_crawl_result_repository(db)
    llm_repository = llm_repo or build_llm_repository(db)
    user_repository = user_repo or build_user_repository(db)

    return URLProcessor(
        cfg=cfg,
        db=db,
        firecrawl=firecrawl,
        openrouter=openrouter,
        response_formatter=response_formatter,
        audit_func=audit_func,
        sem=sem,
        topic_search=topic_search,
        db_write_queue=db_write_queue,
        request_repo=request_repository,
        summary_repo=summary_repository,
        crawl_result_repo=crawl_repository,
        llm_repo=llm_repository,
        user_repo=user_repository,
        related_reads_service=related_reads_service,
        stream_coordinator_factory=SummaryDraftStreamCoordinator,
        platform_router=build_registered_platform_router(
            cfg=cfg,
            db=db,
            scraper=firecrawl,
            response_formatter=response_formatter,
            audit_func=audit_func,
            sem=sem,
            quality_llm_client=openrouter,
        ),
    )


async def close_runtime_resources(*resources: Any) -> None:
    """Close all runtime resources that expose async cleanup hooks."""
    for resource in resources:
        if resource is None:
            continue
        close = getattr(resource, "aclose", None)
        if close is None:
            continue
        try:
            await close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            resource_name = type(resource).__name__
            logger.warning(
                "shutdown_resource_close_failed",
                extra={"resource": resource_name, "error": str(e)},
            )


def _build_firecrawl_client(
    cfg: AppConfig,
    audit: Callable[[str, str, dict[str, Any]], None],
) -> FirecrawlClient | None:
    """Build a FirecrawlClient for the self-hosted instance; returns None when disabled."""
    if not cfg.scraper.firecrawl_self_hosted_enabled:
        return None

    from app.adapters.external.firecrawl.client import FirecrawlClientConfig

    client_cfg = FirecrawlClientConfig(
        timeout_sec=cfg.scraper.firecrawl_timeout_sec,
        max_retries=cfg.scraper.firecrawl_max_retries,
        backoff_base=cfg.firecrawl.retry_initial_delay,
        debug_payloads=cfg.runtime.debug_payloads,
        log_truncate_length=cfg.runtime.log_truncate_length,
        max_connections=cfg.scraper.firecrawl_max_connections,
        max_keepalive_connections=cfg.scraper.firecrawl_max_keepalive_connections,
        keepalive_expiry=cfg.scraper.firecrawl_keepalive_expiry,
        max_response_size_mb=cfg.scraper.firecrawl_max_response_size_mb,
        max_age_seconds=cfg.firecrawl.max_age_seconds,
        remove_base64_images=cfg.firecrawl.remove_base64_images,
        block_ads=cfg.firecrawl.block_ads,
        skip_tls_verification=cfg.firecrawl.skip_tls_verification,
        include_markdown_format=cfg.firecrawl.include_markdown_format,
        include_html_format=cfg.firecrawl.include_html_format,
        include_links_format=cfg.firecrawl.include_links_format,
        include_summary_format=cfg.firecrawl.include_summary_format,
        include_images_format=cfg.firecrawl.include_images_format,
        enable_screenshot_format=cfg.firecrawl.enable_screenshot_format,
        screenshot_full_page=cfg.firecrawl.screenshot_full_page,
        screenshot_quality=cfg.firecrawl.screenshot_quality,
        screenshot_viewport_width=cfg.firecrawl.screenshot_viewport_width,
        screenshot_viewport_height=cfg.firecrawl.screenshot_viewport_height,
        json_prompt=cfg.firecrawl.json_prompt,
        json_schema=cfg.firecrawl.json_schema or {},
        wait_for_ms=cfg.scraper.firecrawl_wait_for_ms,
    )
    return FirecrawlClient(
        cfg.scraper.firecrawl_self_hosted_api_key,
        client_cfg,
        audit=audit,
        base_url=cfg.scraper.firecrawl_self_hosted_url,
    )


def _default_audit(level: str, event: str, details: dict[str, Any]) -> None:
    log_level = logging.INFO if level == "info" else logging.ERROR
    logger.log(log_level, event, extra=details)
