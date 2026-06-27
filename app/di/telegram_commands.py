from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.telegram.command_dispatch import (
    AliasCommandRoute,
    CommandContextFactory,
    TelegramCommandContribution,
    TelegramCommandRuntimeState,
    TextCommandRoute,
    UidCommandRoute,
    merge_command_contributions,
)
from app.adapters.telegram.command_handlers.admin_handler import AdminHandler
from app.adapters.telegram.command_handlers.aggregation_commands_handler import (
    AggregationCommandsHandler,
)
from app.adapters.telegram.command_handlers.ai_backup_handler import AiBackupHandler
from app.adapters.telegram.command_handlers.backup_handler import BackupHandler
from app.adapters.telegram.command_handlers.browse_handler import BrowseHandler
from app.adapters.telegram.command_handlers.content_handler import ContentHandler
from app.adapters.telegram.command_handlers.digest_handler import DigestHandler
from app.adapters.telegram.command_handlers.export_command import ExportHandler
from app.adapters.telegram.command_handlers.git_mirror_handler import GitMirrorHandler
from app.adapters.telegram.command_handlers.init_session_handler import InitSessionHandler
from app.adapters.telegram.command_handlers.listen_handler import ListenHandler
from app.adapters.telegram.command_handlers.onboarding_handler import OnboardingHandler
from app.adapters.telegram.command_handlers.rss_handler import RSSHandler
from app.adapters.telegram.command_handlers.rules_handler import RulesHandler
from app.adapters.telegram.command_handlers.search_handler import SearchHandler
from app.adapters.telegram.command_handlers.settings_handler import SettingsHandler
from app.adapters.telegram.command_handlers.social_handler import SocialHandler
from app.adapters.telegram.command_handlers.tag_handler import TagHandler
from app.adapters.telegram.command_handlers.transcribe_handler import TranscribeHandler
from app.adapters.telegram.command_handlers.url_commands_handler import URLCommandsHandler
from app.adapters.telegram.command_handlers.x_possible import (
    XPossibleHandler,
)
from app.adapters.transcription import TranscriptionService
from app.adapters.webwright.client import WebwrightClient
from app.di.repositories import (
    build_backup_repository,
    build_rss_feed_repository,
    build_rule_repository,
    build_social_connection_repository,
    build_transcription_repository,
    build_user_content_repository,
)
from app.di.social import build_social_auth_service
from app.di.types import TelegramCommandDispatcherDeps, TelegramRepositories

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from app.adapters.ai_backup.repository import AiBackupRepository
    from app.adapters.content.graph_url_processor import GraphURLProcessor as URLProcessor
    from app.adapters.digest.digest_service import DigestService
    from app.adapters.git_backup.repository import GitMirrorRepository
    from app.adapters.telegram.command_handlers.execution_context import CommandExecutionContext
    from app.adapters.telegram.task_manager import UserTaskManager
    from app.adapters.telegram.url_handler import URLHandler
    from app.application.services.social_auth_service import SocialAuthService
    from app.application.services.transcription_job_service import TranscriptionJobService
    from app.config import AppConfig
    from app.db.session import Database


def _build_digest_service_factory(
    cfg: AppConfig,
    formatter: Any,
) -> Callable[[CommandExecutionContext], AbstractAsyncContextManager[DigestService]]:
    """Return an async-context-manager factory for DigestService.

    Lives in the DI layer so the ``telegram`` adapter does not need a runtime
    import of any ``digest`` adapter module.
    """
    from contextlib import asynccontextmanager
    from pathlib import Path as _Path

    from app.adapters.digest.analyzer import DigestAnalyzer
    from app.adapters.digest.channel_reader import ChannelReader
    from app.adapters.digest.digest_service import DigestService
    from app.adapters.digest.formatter import DigestFormatter
    from app.adapters.digest.userbot_client import UserbotClient
    from app.adapters.openrouter.openrouter_client import OpenRouterClient

    @asynccontextmanager
    async def _factory(ctx: CommandExecutionContext):
        session_dir = _Path("/data")
        userbot = UserbotClient(cfg, session_dir)
        await userbot.start()
        try:
            llm_client = OpenRouterClient(
                api_key=cfg.openrouter.api_key,
                model=cfg.openrouter.model,
                fallback_models=cfg.openrouter.fallback_models,
            )
            reader = ChannelReader(cfg, userbot)
            analyzer = DigestAnalyzer(cfg, llm_client)
            digest_formatter = DigestFormatter()

            async def send_msg(user_id: int, text: str, reply_markup: object = None) -> None:
                await formatter.safe_reply(ctx.message, text, reply_markup=reply_markup)

            service = DigestService(
                cfg=cfg,
                reader=reader,
                analyzer=analyzer,
                formatter=digest_formatter,
                send_message_func=send_msg,
            )
            yield service
            await llm_client.aclose()
        finally:
            await userbot.stop()

    return _factory


def _build_ai_backup_repo_factory(db: Any) -> Callable[[], AiBackupRepository]:
    """Return a factory that creates an AiBackupRepository.

    Lives in the DI layer so the ``telegram`` adapter does not need a runtime
    import of any ``ai_backup`` adapter module.
    """
    from app.adapters.ai_backup.repository import AiBackupRepository as _AiBackupRepository

    def _factory() -> _AiBackupRepository:
        return _AiBackupRepository(db)

    return _factory


def _build_mirror_repo_factory(cfg: AppConfig, db: Any) -> Callable[[], GitMirrorRepository]:
    """Return a factory that creates a GitMirrorRepository.

    Lives in the DI layer so the ``telegram`` adapter does not need a runtime
    import of any ``git_backup`` adapter module.
    """
    from app.adapters.git_backup.repository import GitMirrorRepository as _GitMirrorRepository

    def _factory() -> _GitMirrorRepository:
        return _GitMirrorRepository(db, cfg.git_backup)

    return _factory


def build_command_dispatcher_deps(
    *,
    cfg: AppConfig,
    db: Database,
    response_formatter: Any,
    audit_func: Any,
    url_processor: URLProcessor,
    url_handler: URLHandler | None,
    aggregation_handler: Any | None,
    topic_searcher: Any | None,
    local_searcher: Any | None,
    task_manager: UserTaskManager | None,
    hybrid_search: Any | None,
    verbosity_resolver: Any | None,
    application_services: Any | None,
    repositories: TelegramRepositories,
    tts_service_factory: Any | None,
    transcription_service: TranscriptionService | None = None,
    transcription_job_service: TranscriptionJobService | None = None,
) -> TelegramCommandDispatcherDeps:
    runtime_state = TelegramCommandRuntimeState(
        url_processor=url_processor,
        url_handler=url_handler,
        aggregation_handler=aggregation_handler,
        topic_searcher=topic_searcher,
        local_searcher=local_searcher,
        _task_manager=task_manager,
        hybrid_search=hybrid_search,
    )
    context_factory = CommandContextFactory(
        user_repo=repositories.user_repository,
        response_formatter=response_formatter,
        audit_func=audit_func,
    )

    onboarding_handler = OnboardingHandler(response_formatter)
    admin_handler = AdminHandler(
        db=db,
        response_formatter=response_formatter,
        url_processor=url_processor,
        url_handler=url_handler,
        cfg=cfg,
    )
    aggregation_commands_handler = AggregationCommandsHandler(aggregation_handler)
    url_commands_handler = URLCommandsHandler(
        response_formatter=response_formatter,
        processor_provider=runtime_state,
        request_repo=repositories.request_repository,
    )
    content_handler = ContentHandler(
        response_formatter=response_formatter,
        summary_repo=repositories.summary_repository,
        llm_repo=repositories.llm_repository,
        unread_summaries_use_case=getattr(application_services, "unread_summaries", None),
        mark_summary_as_read_use_case=getattr(application_services, "mark_summary_as_read", None),
    )
    search_handler = SearchHandler(
        response_formatter=response_formatter,
        searcher_provider=runtime_state,
        search_topics_use_case=getattr(application_services, "search_topics", None),
    )
    listen_handler = ListenHandler(
        cfg=cfg,
        db=db,
        response_formatter=response_formatter,
        tts_service_factory=tts_service_factory,
        request_repo=repositories.request_repository,
        summary_repo=repositories.summary_repository,
    )
    digest_handler = DigestHandler(
        cfg=cfg,
        db=db,
        response_formatter=response_formatter,
        digest_service_factory=_build_digest_service_factory(cfg, response_formatter),
    )
    init_session_handler = InitSessionHandler(
        cfg=cfg,
        response_formatter=response_formatter,
    )
    social_handler = SocialHandler(
        getattr(application_services, "social_auth", None)
        or _build_social_auth_service(cfg=cfg, db=db)
    )
    settings_handler = SettingsHandler(
        verbosity_resolver=verbosity_resolver,
        cfg=cfg,
    )
    tag_handler = TagHandler(
        cfg=cfg,
        db=db,
        response_formatter=response_formatter,
        tag_repo=repositories.tag_repository,
        request_repo=repositories.request_repository,
        summary_repo=repositories.summary_repository,
    )
    rules_handler = RulesHandler(
        cfg=cfg,
        db=db,
        response_formatter=response_formatter,
        rule_repo_factory=lambda: build_rule_repository(db),
    )
    export_handler = ExportHandler(
        cfg=cfg,
        db=db,
        response_formatter=response_formatter,
        user_content_repo_factory=lambda: build_user_content_repository(db),
    )
    backup_handler = BackupHandler(
        cfg=cfg,
        db=db,
        response_formatter=response_formatter,
        backup_repo_factory=lambda: build_backup_repository(db),
    )
    rss_handler = RSSHandler(
        cfg=cfg,
        db=db,
        response_formatter=response_formatter,
        rss_repo_factory=lambda: build_rss_feed_repository(db),
    )
    git_mirror_handler = GitMirrorHandler(
        cfg=cfg,
        db=db,
        response_formatter=response_formatter,
        mirror_repo_factory=_build_mirror_repo_factory(cfg, db),
    )
    ai_backup_handler = AiBackupHandler(
        cfg=cfg,
        db=db,
        response_formatter=response_formatter,
        ai_backup_repo_factory=_build_ai_backup_repo_factory(db),
    )
    x_possible_handler = XPossibleHandler(cfg=cfg)

    # Webwright `/browse` command wiring. The sidecar URL + tuning knobs live
    # on the scraper config (single source of truth used by both the scraper
    # provider and `/browse`).
    webwright_client = WebwrightClient(
        url=getattr(cfg.scraper, "webwright_url", "http://webwright:8090"),
        timeout_sec=getattr(cfg.scraper, "webwright_timeout_sec", 180),
        default_max_steps=getattr(cfg.scraper, "webwright_max_steps", 20),
    )
    browse_handler = BrowseHandler(
        db=db,
        response_formatter=response_formatter,
        webwright_client=webwright_client,
    )

    transcribe_handler: TranscribeHandler | None = None
    if cfg.transcription.enabled:
        service = transcription_service or TranscriptionService(cfg.transcription)
        transcription_repository = build_transcription_repository(db)
        transcribe_handler = TranscribeHandler(
            cfg=cfg,
            response_formatter=response_formatter,
            transcription_service=service,
            transcription_repository=transcription_repository,
            transcription_job_service=transcription_job_service,
        )

    contributions = (
        TelegramCommandContribution(
            name="onboarding",
            pre_alias_uid=(
                UidCommandRoute(
                    "/start", _build_uid_handler(context_factory, onboarding_handler.handle_start)
                ),
                UidCommandRoute(
                    "/help", _build_uid_handler(context_factory, onboarding_handler.handle_help)
                ),
            ),
        ),
        TelegramCommandContribution(
            name="admin",
            pre_alias_uid=(
                UidCommandRoute(
                    "/dbinfo", _build_uid_handler(context_factory, admin_handler.handle_dbinfo)
                ),
                UidCommandRoute(
                    "/dbverify",
                    _build_uid_handler(context_factory, admin_handler.handle_dbverify),
                ),
                UidCommandRoute(
                    "/models", _build_uid_handler(context_factory, admin_handler.handle_models)
                ),
                UidCommandRoute(
                    "/setmodel",
                    _build_uid_handler(context_factory, admin_handler.handle_setmodel),
                ),
                UidCommandRoute(
                    "/clearcache",
                    _build_uid_handler(context_factory, admin_handler.handle_clearcache),
                ),
            ),
            pre_alias_text=(
                TextCommandRoute(
                    "/admin", _build_text_handler(context_factory, admin_handler.handle_admin)
                ),
            ),
        ),
        TelegramCommandContribution(
            name="aggregation",
            pre_summarize_text=(
                TextCommandRoute(
                    "/aggregate",
                    _build_text_handler(
                        context_factory, aggregation_commands_handler.handle_aggregate
                    ),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="webwright",
            pre_summarize_text=(
                TextCommandRoute(
                    "/browse",
                    _build_text_handler(context_factory, browse_handler.handle_browse),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="url",
            pre_summarize_text=(
                TextCommandRoute(
                    "/summarize_all",
                    _build_text_handler(context_factory, url_commands_handler.handle_summarize_all),
                ),
                TextCommandRoute(
                    "/retry",
                    _build_text_handler(context_factory, url_commands_handler.handle_retry),
                ),
            ),
            summarize_prefix="/summarize",
            post_summarize_uid=(
                UidCommandRoute(
                    "/cancel",
                    _build_uid_handler(context_factory, url_commands_handler.handle_cancel),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="tag",
            post_summarize_text=(
                TextCommandRoute(
                    "/untag", _build_text_handler(context_factory, tag_handler.handle_untag)
                ),
                TextCommandRoute(
                    "/tags", _build_text_handler(context_factory, tag_handler.handle_tags)
                ),
                TextCommandRoute(
                    "/tag", _build_text_handler(context_factory, tag_handler.handle_tag)
                ),
            ),
        ),
        TelegramCommandContribution(
            name="content",
            post_summarize_text=(
                TextCommandRoute(
                    "/unread",
                    _build_text_handler(context_factory, content_handler.handle_unread),
                ),
                TextCommandRoute(
                    "/read", _build_text_handler(context_factory, content_handler.handle_read)
                ),
            ),
        ),
        TelegramCommandContribution(
            name="search",
            local_search_aliases=(
                AliasCommandRoute(
                    ("/finddb", "/findlocal"),
                    _build_alias_handler(context_factory, search_handler.handle_find_local),
                ),
            ),
            online_search_aliases=(
                AliasCommandRoute(
                    ("/findweb", "/findonline", "/find"),
                    _build_alias_handler(context_factory, search_handler.handle_find_online),
                ),
            ),
            post_summarize_text=(
                TextCommandRoute(
                    "/search",
                    _build_text_handler(context_factory, search_handler.handle_search),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="listen",
            post_summarize_text=(
                TextCommandRoute(
                    "/listen",
                    _build_text_handler(context_factory, listen_handler.handle_listen),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="digest",
            post_summarize_text=(
                TextCommandRoute(
                    "/cdigest",
                    _build_text_handler(context_factory, digest_handler.handle_cdigest),
                ),
                TextCommandRoute(
                    "/digest",
                    _build_text_handler(context_factory, digest_handler.handle_digest),
                ),
                TextCommandRoute(
                    "/channels",
                    _build_text_handler(context_factory, digest_handler.handle_channels),
                ),
                TextCommandRoute(
                    "/subscribe",
                    _build_text_handler(context_factory, digest_handler.handle_subscribe),
                ),
                TextCommandRoute(
                    "/unsubscribe",
                    _build_text_handler(context_factory, digest_handler.handle_unsubscribe),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="init_session",
            post_summarize_text=(
                TextCommandRoute(
                    "/init_session",
                    _build_text_handler(context_factory, init_session_handler.handle_init_session),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="social",
            post_summarize_text=(
                TextCommandRoute(
                    "/social", _build_text_handler(context_factory, social_handler.handle_social)
                ),
                TextCommandRoute(
                    "/connect_x",
                    _build_text_handler(context_factory, social_handler.handle_connect_x),
                ),
                TextCommandRoute(
                    "/connect_threads",
                    _build_text_handler(context_factory, social_handler.handle_connect_threads),
                ),
                TextCommandRoute(
                    "/connect_instagram",
                    _build_text_handler(context_factory, social_handler.handle_connect_instagram),
                ),
                TextCommandRoute(
                    "/disconnect_social",
                    _build_text_handler(context_factory, social_handler.handle_disconnect_social),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="settings",
            post_summarize_text=(
                TextCommandRoute(
                    "/settings",
                    _build_text_handler(context_factory, settings_handler.handle_settings),
                ),
            ),
            tail_uid=(
                UidCommandRoute(
                    "/debug", _build_uid_handler(context_factory, settings_handler.handle_debug)
                ),
            ),
        ),
        TelegramCommandContribution(
            name="rules",
            post_summarize_text=(
                TextCommandRoute(
                    "/rules", _build_text_handler(context_factory, rules_handler.handle_rules)
                ),
            ),
        ),
        TelegramCommandContribution(
            name="export",
            post_summarize_text=(
                TextCommandRoute(
                    "/export",
                    _build_text_handler(context_factory, export_handler.handle_export),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="backup",
            post_summarize_text=(
                TextCommandRoute(
                    "/backups",
                    _build_text_handler(context_factory, backup_handler.handle_backups),
                ),
                TextCommandRoute(
                    "/backup",
                    _build_text_handler(context_factory, backup_handler.handle_backup),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="rss",
            post_summarize_text=(
                TextCommandRoute(
                    "/substack",
                    _build_text_handler(context_factory, rss_handler.handle_substack),
                ),
                TextCommandRoute(
                    "/rss", _build_text_handler(context_factory, rss_handler.handle_rss)
                ),
            ),
        ),
        TelegramCommandContribution(
            name="git_mirror",
            post_summarize_text=(
                TextCommandRoute(
                    "/mirror",
                    _build_text_handler(context_factory, git_mirror_handler.handle_mirror),
                ),
                TextCommandRoute(
                    "/mirrors",
                    _build_text_handler(context_factory, git_mirror_handler.handle_mirrors),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="ai_backup",
            post_summarize_text=(
                TextCommandRoute(
                    "/ai_backup",
                    _build_text_handler(context_factory, ai_backup_handler.handle_ai_backup),
                ),
                TextCommandRoute(
                    "/ai_backups",
                    _build_text_handler(context_factory, ai_backup_handler.handle_ai_backups),
                ),
                TextCommandRoute(
                    "/ai_backup_login",
                    _build_text_handler(context_factory, ai_backup_handler.handle_ai_backup_login),
                ),
            ),
        ),
        TelegramCommandContribution(
            name="x_possible",
            post_summarize_text=(
                TextCommandRoute(
                    "/x_possible",
                    _build_text_handler(
                        context_factory,
                        x_possible_handler.handle_x_possible,
                    ),
                ),
            ),
        ),
        *(
            (
                TelegramCommandContribution(
                    name="transcribe",
                    post_summarize_text=(
                        TextCommandRoute(
                            "/transcribe",
                            _build_text_handler(
                                context_factory, transcribe_handler.handle_transcribe
                            ),
                        ),
                    ),
                ),
            )
            if transcribe_handler is not None
            else ()
        ),
    )
    routes = merge_command_contributions(contributions)

    return TelegramCommandDispatcherDeps(
        routes=routes,
        runtime_state=runtime_state,
        context_factory=context_factory,
        onboarding_handler=onboarding_handler,
        admin_handler=admin_handler,
        aggregation_commands_handler=aggregation_commands_handler,
        url_commands_handler=url_commands_handler,
        content_handler=content_handler,
        search_handler=search_handler,
        listen_handler=listen_handler,
        digest_handler=digest_handler,
        init_session_handler=init_session_handler,
        social_handler=social_handler,
        settings_handler=settings_handler,
        tag_handler=tag_handler,
        rules_handler=rules_handler,
        export_handler=export_handler,
        backup_handler=backup_handler,
        transcribe_handler=transcribe_handler,
    )


def _build_social_auth_service(*, cfg: AppConfig, db: Database) -> SocialAuthService:
    return build_social_auth_service(cfg, build_social_connection_repository(db))


def _build_uid_handler(context_factory: CommandContextFactory, handler_method: Any) -> Any:
    async def _handler(
        message: Any,
        uid: int,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> None:
        ctx = context_factory.build(
            message=message,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        )
        await handler_method(ctx)

    return _handler


def _build_text_handler(context_factory: CommandContextFactory, handler_method: Any) -> Any:
    async def _handler(
        message: Any,
        text: str,
        uid: int,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> None:
        ctx = context_factory.build(
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        )
        await handler_method(ctx)

    return _handler


def _build_alias_handler(context_factory: CommandContextFactory, handler_method: Any) -> Any:
    async def _handler(
        message: Any,
        text: str,
        uid: int,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
        command: str,
    ) -> None:
        ctx = context_factory.build(
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        )
        await handler_method(ctx, command=command)

    return _handler
