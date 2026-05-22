from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

from app.api.background import (
    DurableRequestProcessingQueue,
    ProgressEventRepository,
    RequestProcessingJobRepository,
)
from app.api.background_processor import BackgroundProcessor
from app.api.services.sync import (
    FallbackSyncSessionStore,
    InMemorySyncSessionStore,
    RedisSyncSessionStore,
    SyncApplyService,
    SyncEnvelopeSerializer,
    SyncRecordCollector,
)
from app.api.services.sync_service import SyncService
from app.application.services.request_service import RequestService
from app.application.use_cases.search_read_model import SearchReadModelUseCase
from app.application.use_cases.summary_read_model import SummaryReadModelUseCase
from app.config import load_config
from app.core.logging_utils import get_logger
from app.di.database import build_runtime_database
from app.di.repositories import (
    build_crawl_result_repository,
    build_llm_repository,
    build_request_repository,
    build_rss_feed_repository,
    build_summary_repository,
    build_tag_repository,
    build_topic_search_repository,
    build_user_repository,
)
from app.di.search import build_search_dependencies
from app.di.shared import (
    build_async_audit_sink,
    build_core_dependencies,
    build_url_processor,
    close_runtime_resources,
)
from app.di.types import ApiRuntime, DatabaseRuntimeServices, SyncDeps
from app.infrastructure.persistence.sync_aux_read_adapter import SyncAuxReadAdapter
from app.infrastructure.redis import get_redis

if TYPE_CHECKING:
    from fastapi import Request

    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)

# Holder list lets the setters mutate without `global` — eliminates
# the three legacy declarations called out as the most dangerous
# leak in [[eliminate-module-globals]].
_current_runtime_holder: list[ApiRuntime | None] = [None]
_runtime_lock = asyncio.Lock()


async def build_api_runtime(
    cfg: AppConfig | None = None,
    *,
    db: Database | None = None,
    redis_client: Any | None = None,
) -> ApiRuntime:
    """Build the shared API runtime graph."""
    app_cfg = cfg or load_config(allow_stub_telegram=True)
    if not app_cfg.telegram.allowed_user_ids:
        logger.warning(
            "api_access_control_unconfigured",
            extra={
                "detail": (
                    "ALLOWED_USER_IDS is not set — JWT-backed API and hosted MCP auth "
                    "run in explicit fail-open multi-user mode by design. Set "
                    "ALLOWED_USER_IDS to enforce a whitelist. Secret-login onboarding "
                    "and Telegram bot access remain separately constrained."
                )
            },
        )
    # `build_runtime_database(..., connect=True, migrate=True)` calls
    # asyncio.run() internally; doing that from inside an active event loop
    # (e.g. the FastAPI lifespan or any async caller of build_api_runtime)
    # raises "asyncio.run() cannot be called from a running event loop".
    # Build the database without those flags and exercise the equivalents
    # via await once we hold an async context.
    if db is None:
        database = build_runtime_database(app_cfg)
        await database.healthcheck()
        await database.migrate()
    else:
        database = db
    database_services = DatabaseRuntimeServices(
        executor=database.executor,
        bootstrap=database.bootstrap,
        maintenance=database.maintenance,
        inspection=database.inspection,
        backups=database.backups,
    )
    audit_sink = build_async_audit_sink(database)
    core = build_core_dependencies(app_cfg, database, audit_sink=audit_sink)
    search = build_search_dependencies(
        app_cfg,
        database,
        llm_client=core.llm_client,
        audit_func=core.audit_sink,
        firecrawl_client=core.firecrawl_client,
    )
    redis = redis_client if redis_client is not None else await get_redis(app_cfg)
    url_processor = build_url_processor(
        cfg=app_cfg,
        db=database,
        firecrawl=core.scraper_chain,
        openrouter=core.llm_client,
        response_formatter=core.response_formatter,
        audit_func=core.audit_sink,
        sem=core.semaphore_factory,
        topic_search=search.topic_searcher if app_cfg.web_search.enabled else None,
    )

    def url_processor_factory(runtime_db: Any) -> Any:
        return build_url_processor(
            cfg=app_cfg,
            db=runtime_db,
            firecrawl=core.scraper_chain,
            openrouter=core.llm_client,
            response_formatter=core.response_formatter,
            audit_func=core.audit_sink,
            sem=core.semaphore_factory,
            topic_search=search.topic_searcher if app_cfg.web_search.enabled else None,
        )

    user_repository = build_user_repository(database)
    request_repository = build_request_repository(database)
    summary_repository = build_summary_repository(database)
    crawl_result_repository = build_crawl_result_repository(database)
    llm_repository = build_llm_repository(database)
    progress_event_repository = ProgressEventRepository(database)
    background_processor = BackgroundProcessor(
        cfg=app_cfg,
        db=database,
        url_processor=url_processor,
        redis=redis,
        semaphore=core.semaphore_factory(),
        audit_func=core.audit_sink,
        url_processor_factory=url_processor_factory,
        database_builder=lambda override_cfg: build_runtime_database(override_cfg),
        request_repo=request_repository,
        summary_repo=summary_repository,
        request_repo_factory=build_request_repository,
        summary_repo_factory=build_summary_repository,
        progress_event_repo=progress_event_repository,
    )
    durable_request_queue = DurableRequestProcessingQueue(
        repository=RequestProcessingJobRepository(database),
        processor=background_processor,
        max_attempts=app_cfg.background.retry_attempts,
        lease_ttl_seconds=app_cfg.background.durable_lease_ttl_seconds,
        retry_delay_seconds=app_cfg.background.durable_retry_delay_seconds,
        poll_interval_seconds=app_cfg.background.durable_poll_interval_ms / 1000,
        stale_processing_seconds=app_cfg.background.stuck_processing_seconds,
    )
    summary_read_model_use_case = SummaryReadModelUseCase(
        summary_repository=summary_repository,
        request_repository=request_repository,
        crawl_result_repository=crawl_result_repository,
        llm_repository=llm_repository,
    )
    search_read_model_use_case = SearchReadModelUseCase(
        topic_search_repository=build_topic_search_repository(database),
        request_repository=request_repository,
        summary_repository=summary_repository,
    )
    request_service = RequestService(
        db=database,
        request_repository=request_repository,
        summary_repository=summary_repository,
        crawl_result_repository=crawl_result_repository,
        llm_repository=llm_repository,
        progress_event_repository=progress_event_repository,
    )
    sync_serializer = SyncEnvelopeSerializer()
    sync_aux_reads = SyncAuxReadAdapter(database)
    sync_session_store = FallbackSyncSessionStore(
        redis_store=RedisSyncSessionStore(app_cfg),
        fallback_store=InMemorySyncSessionStore(),
    )
    sync_record_collector = SyncRecordCollector(
        user_repository=user_repository,
        request_repository=request_repository,
        summary_repository=summary_repository,
        crawl_result_repository=crawl_result_repository,
        llm_repository=llm_repository,
        aux_read_port=sync_aux_reads,
        serializer=sync_serializer,
    )
    sync_apply_service = SyncApplyService(
        summary_repository=summary_repository,
        serializer=sync_serializer,
    )
    sync_deps = SyncDeps(
        user_repository=user_repository,
        request_repository=request_repository,
        summary_repository=summary_repository,
        crawl_result_repository=crawl_result_repository,
        llm_repository=llm_repository,
        session_store=sync_session_store,
        aux_read_port=sync_aux_reads,
        record_collector=sync_record_collector,
        envelope_serializer=sync_serializer,
        apply_service=sync_apply_service,
    )
    sync_service = SyncService(
        app_cfg,
        database,
        user_repository=sync_deps.user_repository,
        request_repository=sync_deps.request_repository,
        summary_repository=sync_deps.summary_repository,
        crawl_result_repository=sync_deps.crawl_result_repository,
        llm_repository=sync_deps.llm_repository,
        session_store=sync_deps.session_store,
        aux_read_port=sync_deps.aux_read_port,
        record_collector=sync_deps.record_collector,
        envelope_serializer=sync_deps.envelope_serializer,
        apply_service=sync_deps.apply_service,
    )
    tag_repo = build_tag_repository(database)
    rss_feed_repo = build_rss_feed_repository(database)
    return ApiRuntime(
        cfg=app_cfg,
        db=database,
        database_services=database_services,
        redis_client=redis,
        core=core,
        search=search,
        background_processor=background_processor,
        durable_request_queue=durable_request_queue,
        progress_event_repository=progress_event_repository,
        summary_read_model_use_case=summary_read_model_use_case,
        search_read_model_use_case=search_read_model_use_case,
        request_service=request_service,
        sync_service=sync_service,
        tag_repo=tag_repo,
        rss_feed_repo=rss_feed_repo,
    )


async def build_background_processor(
    cfg: AppConfig | None = None,
    *,
    db: Database | None = None,
    redis_client: Any | None = None,
) -> BackgroundProcessor:
    """Compatibility helper for tests that need only the background processor."""
    runtime = await build_api_runtime(cfg, db=db, redis_client=redis_client)
    return cast("BackgroundProcessor", runtime.background_processor)


async def ensure_api_runtime() -> ApiRuntime:
    """Initialize and return the process-wide API runtime if not already running."""
    if _current_runtime_holder[0] is not None:
        return _current_runtime_holder[0]

    async with _runtime_lock:
        if _current_runtime_holder[0] is not None:
            return _current_runtime_holder[0]
        _current_runtime_holder[0] = await build_api_runtime()
        assert _current_runtime_holder[0] is not None
        return _current_runtime_holder[0]


def get_current_api_runtime() -> ApiRuntime:
    """Return the active API runtime, requiring explicit initialization."""
    if _current_runtime_holder[0] is None:
        msg = "API runtime is not initialized"
        raise RuntimeError(msg)
    return _current_runtime_holder[0]


def set_current_api_runtime(runtime: ApiRuntime) -> None:
    _current_runtime_holder[0] = runtime


def clear_current_api_runtime() -> None:
    """Clear the process-wide API runtime (call during shutdown)."""
    _current_runtime_holder[0] = None


def resolve_api_runtime(request: Request | None = None) -> ApiRuntime:
    """Resolve runtime from FastAPI request state or the process-global cache."""
    if request is not None:
        runtime = getattr(request.app.state, "runtime", None)
        if runtime is not None:
            return cast("ApiRuntime", runtime)
    return get_current_api_runtime()


async def close_api_runtime(runtime: ApiRuntime) -> None:
    """Release resources owned by the API runtime."""
    await close_runtime_resources(
        runtime.background_processor.url_processor,
        runtime.search.vector_store,
        runtime.search.embedding_service,
        runtime.core.firecrawl_client,
        runtime.core.llm_client,
    )
    await runtime.db.dispose()
    logger.info("api_runtime_closed")
