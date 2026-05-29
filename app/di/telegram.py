from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.adapters.attachment.attachment_processor import AttachmentProcessor
from app.adapters.telegram.access_controller import AccessController
from app.adapters.telegram.callback_handler import CallbackHandler
from app.adapters.telegram.command_dispatcher import TelegramCommandDispatcher
from app.adapters.telegram.forward_processor import ForwardProcessor
from app.adapters.telegram.message_handler import MessageHandler
from app.adapters.telegram.message_router import MessageRouter
from app.adapters.telegram.multi_source_aggregation_handler import (
    MultiSourceAggregationHandler,
)
from app.adapters.telegram.routing.voice_message_processor import VoiceMessageProcessor
from app.adapters.telegram.task_manager import UserTaskManager
from app.adapters.telegram.telegram_client import TelegramClient
from app.adapters.telegram.url_batch_policy_service import URLBatchPolicyService
from app.adapters.telegram.url_handler import URLHandler
from app.adapters.transcription import TranscriptionService, get_or_create_transcription_service
from app.agents.multi_source_aggregation_agent import MultiSourceAggregationAgent
from app.agents.multi_source_extraction_agent import MultiSourceExtractionAgent
from app.agents.relationship_analysis_agent import RelationshipAnalysisAgent
from app.application.services.adaptive_timeout import AdaptiveTimeoutService
from app.application.services.aggregation_rollout import AggregationRolloutGate
from app.application.services.llm_cascade_timeout import compute_llm_cascade_floor
from app.application.services.multi_source_aggregation_service import (
    MultiSourceAggregationService,
)
from app.application.services.related_reads_service import RelatedReadsService
from app.application.services.transcription_job_service import TranscriptionJobService
from app.application.services.tts_service import TTSService
from app.core.logging_utils import get_logger
from app.core.verbosity import VerbosityResolver
from app.di.application import build_application_services
from app.di.repositories import (
    build_aggregation_session_repository,
    build_audit_log_repository,
    build_batch_session_repository,
    build_crawl_result_repository,
    build_embedding_repository,
    build_llm_repository,
    build_request_repository,
    build_summary_repository,
    build_tag_repository,
    build_topic_search_repository,
    build_transcription_repository,
    build_user_repository,
)
from app.di.search import build_search_dependencies, get_topic_search_limit
from app.di.shared import build_async_audit_sink, build_core_dependencies, build_url_processor
from app.di.telegram_commands import build_command_dispatcher_deps as _build_command_dispatcher_deps
from app.di.types import (
    SummaryCliRuntime,
    TelegramCommandDispatcherDeps,
    TelegramRepositories,
    TelegramRuntime,
)
from app.infrastructure.audio.elevenlabs_provider import ElevenLabsTTSProviderAdapter
from app.infrastructure.audio.filesystem_storage import FileSystemAudioStorageAdapter
from app.infrastructure.persistence.repositories.audio_generation_repository import (
    AudioGenerationRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.latency_stats_repository import (
    LatencyStatsRepositoryAdapter,
)
from app.infrastructure.search.vector_search_port_adapter import VectorSearchPortAdapter
from app.infrastructure.search.vector_search_service import VectorSearchService
from app.security.file_validation import SecureFileValidator

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.db.session import Database
    from app.db.write_queue import DbWriteQueue

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _TelegramProcessingStack:
    url_processor: Any
    forward_processor: Any
    attachment_processor: Any


@dataclass(frozen=True, slots=True)
class _TelegramInterfaceStack:
    telegram_client: TelegramClient
    adaptive_timeout_service: AdaptiveTimeoutService | None
    task_manager: UserTaskManager
    url_handler: URLHandler
    command_dispatcher: TelegramCommandDispatcher
    access_controller: AccessController
    callback_handler: CallbackHandler
    message_handler: MessageHandler
    durable_transcription_queue: TranscriptionJobService | None = None


def build_telegram_runtime(
    cfg: AppConfig,
    db: Database,
    *,
    safe_reply_func: Any,
    reply_json_func: Any,
    db_write_queue: DbWriteQueue | None = None,
    audit_task_registry: set[Any] | None = None,
) -> TelegramRuntime:
    """Build the full Telegram runtime graph from shared DI modules."""
    telegram_repositories = _build_telegram_repositories(db)
    verbosity_resolver = VerbosityResolver(telegram_repositories.user_repository)
    audit_sink = build_async_audit_sink(db, task_registry=audit_task_registry)
    core = build_core_dependencies(
        cfg,
        db,
        audit_sink=audit_sink,
        response_formatter_kwargs={
            "safe_reply_func": safe_reply_func,
            "reply_json_func": reply_json_func,
            "verbosity_resolver": verbosity_resolver,
            "admin_log_chat_id": cfg.telegram.admin_log_chat_id,
        },
    )
    topic_search_max_results = get_topic_search_limit(cfg)
    search = _build_search_stack(
        cfg=cfg,
        db=db,
        llm_client=core.llm_client,
        audit_func=core.audit_sink,
        firecrawl_client=core.firecrawl_client,
        topic_search_max_results=topic_search_max_results,
    )
    processing = _build_processing_stack(
        cfg=cfg,
        db=db,
        core=core,
        search=search,
        repositories=telegram_repositories,
        db_write_queue=db_write_queue,
    )
    application_services = build_application_services(
        db,
        topic_search_service=search.local_searcher,
        vector_store=search.vector_store,
        embedding_generator=search.embedding_generator,
    )
    interface = _build_telegram_interface_stack(
        cfg=cfg,
        db=db,
        core=core,
        search=search,
        repositories=telegram_repositories,
        processing=processing,
        application_services=application_services,
        audit_func=audit_sink,
        verbosity_resolver=verbosity_resolver,
    )

    logger.info(
        "telegram_runtime_initialized",
        extra={
            "topic_search_max_results": topic_search_max_results,
            "vector_search_enabled": search.vector_search_service is not None,
        },
    )
    return TelegramRuntime(
        core=core,
        search=search,
        application_services=application_services,
        telegram_client=interface.telegram_client,
        response_formatter=core.response_formatter,
        url_processor=processing.url_processor,
        forward_processor=processing.forward_processor,
        attachment_processor=processing.attachment_processor,
        message_handler=interface.message_handler,
        durable_transcription_queue=interface.durable_transcription_queue,
        adaptive_timeout_service=interface.adaptive_timeout_service,
        verbosity_resolver=verbosity_resolver,
    )


def build_summary_cli_runtime(
    cfg: AppConfig,
    db: Database,
) -> SummaryCliRuntime:
    """Build the CLI summary runtime using the same shared resources as Telegram."""
    repositories = _build_telegram_repositories(db)
    audit_sink = build_async_audit_sink(db)
    core = build_core_dependencies(cfg, db, audit_sink=audit_sink)
    search = _build_search_stack(
        cfg=cfg,
        db=db,
        llm_client=core.llm_client,
        audit_func=core.audit_sink,
        firecrawl_client=core.firecrawl_client,
        topic_search_max_results=None,
    )
    url_processor = build_url_processor(
        cfg=cfg,
        db=db,
        firecrawl=core.scraper_chain,
        openrouter=core.llm_client,
        response_formatter=core.response_formatter,
        audit_func=core.audit_sink,
        sem=core.semaphore_factory,
        topic_search=search.topic_searcher if cfg.web_search.enabled else None,
        request_repo=repositories.request_repository,
        summary_repo=repositories.summary_repository,
        crawl_result_repo=repositories.crawl_result_repository,
        llm_repo=repositories.llm_repository,
        user_repo=repositories.user_repository,
    )
    application_services = build_application_services(
        db,
        topic_search_service=search.local_searcher,
        vector_store=search.vector_store,
        embedding_generator=search.embedding_generator,
    )
    url_handler = URLHandler(
        db=db,
        response_formatter=core.response_formatter,
        url_processor=url_processor,
        user_repo=repositories.user_repository,
        request_repo=repositories.request_repository,
        cfg=cfg,
    )
    _agg_session_repo_cli = build_aggregation_session_repository(db)
    aggregation_handler = MultiSourceAggregationHandler(
        response_formatter=core.response_formatter,
        workflow_service=MultiSourceAggregationService(
            extraction_agent=MultiSourceExtractionAgent(
                content_extractor=url_processor.content_extractor,
                aggregation_session_repo=_agg_session_repo_cli,
            ),
            aggregation_agent=MultiSourceAggregationAgent(
                aggregation_session_repo=_agg_session_repo_cli,
                llm_client=core.llm_client,
            ),
            aggregation_session_repo=_agg_session_repo_cli,
            relationship_agent=RelationshipAnalysisAgent(
                llm_client=core.llm_client,
            )
            if core.llm_client is not None
            else None,
        ),
        rollout_gate=AggregationRolloutGate(
            cfg=cfg,
            user_repo=repositories.user_repository,
        ),
    )
    dispatcher_deps = _build_command_dispatcher_deps(
        cfg=cfg,
        db=db,
        response_formatter=core.response_formatter,
        audit_func=core.audit_sink,
        url_processor=url_processor,
        url_handler=url_handler,
        aggregation_handler=aggregation_handler,
        topic_searcher=search.topic_searcher,
        local_searcher=search.local_searcher,
        task_manager=None,
        hybrid_search=search.hybrid_search_service,
        verbosity_resolver=None,
        application_services=application_services,
        repositories=repositories,
        tts_service_factory=_build_tts_service_factory(
            cfg=cfg,
            db=db,
            summary_repo=repositories.summary_repository,
        ),
    )
    command_processor = _build_command_dispatcher(dispatcher_deps)
    return SummaryCliRuntime(
        core=core,
        search=search,
        application_services=application_services,
        url_processor=url_processor,
        command_processor=command_processor,
    )


def _build_telegram_repositories(db: Database) -> TelegramRepositories:
    return TelegramRepositories(
        user_repository=build_user_repository(db),
        summary_repository=build_summary_repository(db),
        request_repository=build_request_repository(db),
        crawl_result_repository=build_crawl_result_repository(db),
        llm_repository=build_llm_repository(db),
        tag_repository=build_tag_repository(db),
        audit_log_repository=build_audit_log_repository(db),
        batch_session_repository=build_batch_session_repository(db),
    )


def _build_search_stack(
    *,
    cfg: AppConfig,
    db: Database,
    llm_client: Any,
    audit_func: Any,
    firecrawl_client: Any,
    topic_search_max_results: int | None,
) -> Any:
    return build_search_dependencies(
        cfg,
        db,
        llm_client=llm_client,
        audit_func=audit_func,
        firecrawl_client=firecrawl_client,
        topic_search_max_results=topic_search_max_results,
    )


def _build_processing_stack(
    *,
    cfg: AppConfig,
    db: Database,
    core: Any,
    search: Any,
    repositories: TelegramRepositories,
    db_write_queue: DbWriteQueue | None,
) -> _TelegramProcessingStack:
    related_reads_service = _build_related_reads_service(cfg=cfg, db=db, search=search)
    url_processor = build_url_processor(
        cfg=cfg,
        db=db,
        firecrawl=core.scraper_chain,
        openrouter=core.llm_client,
        response_formatter=core.response_formatter,
        audit_func=core.audit_sink,
        sem=core.semaphore_factory,
        topic_search=search.topic_searcher if cfg.web_search.enabled else None,
        db_write_queue=db_write_queue,
        request_repo=repositories.request_repository,
        summary_repo=repositories.summary_repository,
        crawl_result_repo=repositories.crawl_result_repository,
        llm_repo=repositories.llm_repository,
        user_repo=repositories.user_repository,
        related_reads_service=related_reads_service,
    )
    forward_processor = ForwardProcessor(
        cfg=cfg,
        db=db,
        openrouter=core.llm_client,
        response_formatter=core.response_formatter,
        audit_func=core.audit_sink,
        sem=core.semaphore_factory,
        db_write_queue=db_write_queue,
        summary_repo=repositories.summary_repository,
        request_repo=repositories.request_repository,
        crawl_result_repo=repositories.crawl_result_repository,
        llm_repo=repositories.llm_repository,
        user_repo=repositories.user_repository,
        related_reads_service=related_reads_service,
        # Share the URL flow's content extractor so forwarded posts can scrape
        # the full content of their embedded links into the summary prompt.
        content_extractor=url_processor.content_extractor,
    )
    attachment_processor = AttachmentProcessor(
        cfg=cfg,
        db=db,
        openrouter=core.llm_client,
        response_formatter=core.response_formatter,
        audit_func=core.audit_sink,
        sem=core.semaphore_factory,
        db_write_queue=db_write_queue,
        request_repo=repositories.request_repository,
        summary_repo=repositories.summary_repository,
        llm_repo=repositories.llm_repository,
        user_repo=repositories.user_repository,
    )
    return _TelegramProcessingStack(
        url_processor=url_processor,
        forward_processor=forward_processor,
        attachment_processor=attachment_processor,
    )


def _build_telegram_interface_stack(
    *,
    cfg: AppConfig,
    db: Database,
    core: Any,
    search: Any,
    repositories: TelegramRepositories,
    processing: _TelegramProcessingStack,
    application_services: Any,
    audit_func: Any,
    verbosity_resolver: VerbosityResolver,
) -> _TelegramInterfaceStack:
    telegram_client = TelegramClient(cfg=cfg)
    core.response_formatter.set_telegram_client(telegram_client)
    _configure_forum_topics(
        cfg=cfg,
        response_formatter=core.response_formatter,
        telegram_client=telegram_client,
    )

    adaptive_timeout_service = _create_adaptive_timeout_service(cfg=cfg, db=db)
    task_manager = UserTaskManager()
    url_handler = URLHandler(
        db=db,
        response_formatter=core.response_formatter,
        url_processor=processing.url_processor,
        adaptive_timeout_service=adaptive_timeout_service,
        verbosity_resolver=verbosity_resolver,
        llm_client=core.llm_client,
        batch_session_repo=repositories.batch_session_repository,
        batch_config=cfg.batch_analysis,
        user_repo=repositories.user_repository,
        request_repo=repositories.request_repository,
        file_validator=SecureFileValidator(max_file_size=10 * 1024 * 1024),
        batch_policy=URLBatchPolicyService(
            floor_sec=compute_llm_cascade_floor(cfg),
        ),
        cfg=cfg,
    )
    _agg_session_repo = build_aggregation_session_repository(db)
    aggregation_handler = MultiSourceAggregationHandler(
        response_formatter=core.response_formatter,
        workflow_service=MultiSourceAggregationService(
            extraction_agent=MultiSourceExtractionAgent(
                content_extractor=processing.url_processor.content_extractor,
                aggregation_session_repo=_agg_session_repo,
            ),
            aggregation_agent=MultiSourceAggregationAgent(
                aggregation_session_repo=_agg_session_repo,
                llm_client=core.llm_client,
            ),
            aggregation_session_repo=_agg_session_repo,
            relationship_agent=RelationshipAnalysisAgent(
                llm_client=core.llm_client,
            )
            if core.llm_client is not None
            else None,
        ),
        rollout_gate=AggregationRolloutGate(
            cfg=cfg,
            user_repo=repositories.user_repository,
        ),
        lang=getattr(core.response_formatter, "_lang", "en"),
    )
    transcription_service: TranscriptionService | None = None
    transcription_job_service: TranscriptionJobService | None = None
    voice_processor: VoiceMessageProcessor | None = None
    if cfg.transcription.enabled:
        transcription_service = get_or_create_transcription_service(cfg.transcription)
        transcription_repository = build_transcription_repository(db)
        transcription_job_service = TranscriptionJobService(
            repository=transcription_repository,
            transcription_service=transcription_service,
            cfg=cfg.transcription,
            max_attempts=cfg.background.retry_attempts,
            lease_ttl_seconds=cfg.background.durable_lease_ttl_seconds,
            retry_delay_seconds=cfg.background.durable_retry_delay_seconds,
            poll_interval_seconds=cfg.background.durable_poll_interval_ms / 1000,
            telegram_media_downloader=_build_telegram_media_downloader(telegram_client),
            url_media_downloader=_build_url_media_downloader(),
        )
        if cfg.transcription.auto_on_voice_message:
            voice_processor = VoiceMessageProcessor(
                response_formatter=core.response_formatter,
                transcription_service=transcription_service,
                diarization_enabled=cfg.transcription.diarization_enabled,
                transcription_cfg=cfg.transcription,
                transcription_repository=transcription_repository,
                transcription_job_service=transcription_job_service,
            )
    dispatcher_deps = _build_command_dispatcher_deps(
        cfg=cfg,
        db=db,
        response_formatter=core.response_formatter,
        audit_func=audit_func,
        url_processor=processing.url_processor,
        url_handler=url_handler,
        aggregation_handler=aggregation_handler,
        topic_searcher=search.topic_searcher,
        local_searcher=search.local_searcher,
        task_manager=task_manager,
        hybrid_search=search.hybrid_search_service,
        verbosity_resolver=verbosity_resolver,
        application_services=application_services,
        repositories=repositories,
        tts_service_factory=_build_tts_service_factory(
            cfg=cfg,
            db=db,
            summary_repo=repositories.summary_repository,
        ),
        transcription_service=transcription_service,
        transcription_job_service=transcription_job_service,
    )
    command_dispatcher = _build_command_dispatcher(dispatcher_deps)
    access_controller = AccessController(
        cfg=cfg,
        db=db,
        response_formatter=core.response_formatter,
        audit_func=audit_func,
        user_repo=repositories.user_repository,
    )
    lang = getattr(core.response_formatter, "_lang", "en")
    callback_handler = CallbackHandler(
        db=db,
        response_formatter=core.response_formatter,
        url_handler=url_handler,
        hybrid_search=search.hybrid_search_service,
        lang=lang,
    )
    message_router = MessageRouter(
        cfg=cfg,
        db=db,
        access_controller=access_controller,
        command_processor=command_dispatcher,
        url_handler=url_handler,
        forward_processor=processing.forward_processor,
        response_formatter=core.response_formatter,
        audit_func=audit_func,
        task_manager=task_manager,
        attachment_processor=processing.attachment_processor,
        aggregation_handler=aggregation_handler,
        user_repo=repositories.user_repository,
        callback_handler=callback_handler,
        voice_processor=voice_processor,
        lang=lang,
    )
    message_handler = MessageHandler(
        cfg=cfg,
        db=db,
        audit_repo=repositories.audit_log_repository,
        task_manager=task_manager,
        access_controller=access_controller,
        url_handler=url_handler,
        command_dispatcher=command_dispatcher,
        callback_handler=callback_handler,
        message_router=message_router,
        url_processor=processing.url_processor,
    )
    return _TelegramInterfaceStack(
        telegram_client=telegram_client,
        adaptive_timeout_service=adaptive_timeout_service,
        task_manager=task_manager,
        url_handler=url_handler,
        command_dispatcher=command_dispatcher,
        access_controller=access_controller,
        callback_handler=callback_handler,
        message_handler=message_handler,
        durable_transcription_queue=transcription_job_service,
    )


def _build_tts_service_factory(
    *,
    cfg: AppConfig,
    db: Database,
    summary_repo: Any,
) -> Any:
    return lambda: TTSService(
        summary_repository=summary_repo,
        audio_generation_repository=AudioGenerationRepositoryAdapter(db),
        tts_provider=ElevenLabsTTSProviderAdapter(cfg.tts),
        audio_storage=FileSystemAudioStorageAdapter(cfg.tts.audio_storage_path),
        voice_id=cfg.tts.voice_id,
        model_name=cfg.tts.model,
        max_chars_per_request=cfg.tts.max_chars_per_request,
    )


def _build_command_dispatcher(
    dispatcher_deps: TelegramCommandDispatcherDeps,
) -> TelegramCommandDispatcher:
    return TelegramCommandDispatcher(
        routes=dispatcher_deps.routes,
        runtime_state=dispatcher_deps.runtime_state,
        context_factory=dispatcher_deps.context_factory,
        onboarding_handler=dispatcher_deps.onboarding_handler,
        admin_handler=dispatcher_deps.admin_handler,
        aggregation_commands_handler=dispatcher_deps.aggregation_commands_handler,
        url_commands_handler=dispatcher_deps.url_commands_handler,
        content_handler=dispatcher_deps.content_handler,
        search_handler=dispatcher_deps.search_handler,
        listen_handler=dispatcher_deps.listen_handler,
        digest_handler=dispatcher_deps.digest_handler,
        init_session_handler=dispatcher_deps.init_session_handler,
        settings_handler=dispatcher_deps.settings_handler,
        tag_handler=dispatcher_deps.tag_handler,
        rules_handler=dispatcher_deps.rules_handler,
        export_handler=dispatcher_deps.export_handler,
        backup_handler=dispatcher_deps.backup_handler,
        transcribe_handler=dispatcher_deps.transcribe_handler,
    )


def _configure_forum_topics(
    *,
    cfg: AppConfig,
    response_formatter: Any,
    telegram_client: TelegramClient,
) -> None:
    if not cfg.telegram.forum_topics_enabled:
        return
    from app.adapters.telegram.topic_manager import TopicManager

    topic_manager = TopicManager()
    response_formatter.set_topic_manager(topic_manager)
    telegram_client.topic_manager = topic_manager
    logger.info("forum_topic_manager_initialized")


def _build_url_media_downloader() -> Any:
    import asyncio

    from app.adapters.transcription import fetch_url_to_local_sync

    async def _download(url: str, workdir: Any) -> Any:
        return await asyncio.to_thread(fetch_url_to_local_sync, url, workdir)

    return _download


def _build_telegram_media_downloader(telegram_client: TelegramClient) -> Any:
    async def _download(job: Any, workdir: Any) -> Any:
        client = getattr(telegram_client, "client", None)
        raw = getattr(client, "raw", None)
        if raw is None:
            msg = "telegram client is not available for transcription download"
            raise RuntimeError(msg)
        if job.telegram_chat_id is None or job.telegram_message_id is None:
            msg = "telegram transcription job is missing chat/message identifiers"
            raise RuntimeError(msg)
        message = await raw.get_messages(job.telegram_chat_id, ids=job.telegram_message_id)
        if message is None:
            msg = "telegram message was not found for transcription"
            raise RuntimeError(msg)
        saved = await message.download_media(file=str(workdir) + "/")
        if saved is None:
            msg = "telegram returned no media path for transcription"
            raise RuntimeError(msg)
        return saved

    return _download


def _create_adaptive_timeout_service(
    *,
    cfg: AppConfig,
    db: Database,
) -> AdaptiveTimeoutService | None:
    if cfg.adaptive_timeout is None:
        return None
    try:
        service = AdaptiveTimeoutService(
            config=cfg.adaptive_timeout,
            repository=LatencyStatsRepositoryAdapter(db),
        )
        logger.info(
            "adaptive_timeout_service_initialized",
            extra={
                "enabled": cfg.adaptive_timeout.enabled,
                "default_timeout_sec": cfg.adaptive_timeout.default_timeout_sec,
                "min_timeout_sec": cfg.adaptive_timeout.min_timeout_sec,
                "max_timeout_sec": cfg.adaptive_timeout.max_timeout_sec,
            },
        )
        return service
    except Exception as exc:
        logger.warning(
            "adaptive_timeout_service_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
            exc_info=True,
        )
        return None


def _build_related_reads_service(
    *,
    cfg: AppConfig,
    db: Database,
    search: Any,
) -> RelatedReadsService | None:
    if not cfg.runtime.related_reads_enabled:
        return None
    try:
        vector_search_service = VectorSearchService(
            embedding_repository=build_embedding_repository(db),
            topic_search_repository=build_topic_search_repository(db),
            embedding_service=search.embedding_service,
            max_results=10,
            min_similarity=0.3,
        )
        related_reads_service = RelatedReadsService(
            VectorSearchPortAdapter(vector_search_service),
            min_similarity=cfg.runtime.related_reads_min_similarity,
        )
        logger.info("related_reads_service_initialized")
        return related_reads_service
    except Exception as exc:
        logger.warning(
            "related_reads_service_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
            exc_info=True,
        )
        return None
