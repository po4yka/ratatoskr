# ruff: noqa: TC001
"""Telegram command dispatcher."""

from __future__ import annotations

from typing import Any

from app.adapters.telegram.command_dispatch import (
    CommandContextFactory,
    CommandDispatchOutcome,
    TelegramCommandRoutes,
    TelegramCommandRuntimeState,
    dispatch_alias_routes,
    dispatch_summarize_fallback,
    dispatch_text_routes,
    dispatch_uid_routes,
)
from app.adapters.telegram.command_handlers.admin_handler import AdminHandler
from app.adapters.telegram.command_handlers.aggregation_commands_handler import (
    AggregationCommandsHandler,
)
from app.adapters.telegram.command_handlers.backup_handler import BackupHandler
from app.adapters.telegram.command_handlers.content_handler import ContentHandler
from app.adapters.telegram.command_handlers.digest_handler import DigestHandler
from app.adapters.telegram.command_handlers.export_command import ExportHandler
from app.adapters.telegram.command_handlers.init_session_handler import InitSessionHandler
from app.adapters.telegram.command_handlers.listen_handler import ListenHandler
from app.adapters.telegram.command_handlers.onboarding_handler import OnboardingHandler
from app.adapters.telegram.command_handlers.rules_handler import RulesHandler
from app.adapters.telegram.command_handlers.search_handler import SearchHandler
from app.adapters.telegram.command_handlers.settings_handler import SettingsHandler
from app.adapters.telegram.command_handlers.tag_handler import TagHandler
from app.adapters.telegram.command_handlers.transcribe_handler import TranscribeHandler
from app.adapters.telegram.command_handlers.url_commands_handler import URLCommandsHandler
from app.adapters.telegram.command_handlers.utils import maybe_load_json


class TelegramCommandDispatcher:
    """Thin coordinator for Telegram command precedence."""

    def __init__(
        self,
        *,
        routes: TelegramCommandRoutes,
        runtime_state: TelegramCommandRuntimeState,
        context_factory: CommandContextFactory,
        onboarding_handler: OnboardingHandler,
        admin_handler: AdminHandler,
        aggregation_commands_handler: AggregationCommandsHandler,
        url_commands_handler: URLCommandsHandler,
        content_handler: ContentHandler,
        search_handler: SearchHandler,
        listen_handler: ListenHandler,
        digest_handler: DigestHandler,
        init_session_handler: InitSessionHandler,
        settings_handler: SettingsHandler,
        tag_handler: TagHandler,
        rules_handler: RulesHandler,
        export_handler: ExportHandler,
        backup_handler: BackupHandler,
        transcribe_handler: TranscribeHandler | None = None,
    ) -> None:
        self._routes = routes
        self._runtime_state = runtime_state
        self._context_factory = context_factory
        self._onboarding = onboarding_handler
        self._admin = admin_handler
        self._aggregation = aggregation_commands_handler
        self._url_commands = url_commands_handler
        self._content = content_handler
        self._search = search_handler
        self._listen = listen_handler
        self._digest = digest_handler
        self._init_session = init_session_handler
        self._settings = settings_handler
        self._tag = tag_handler
        self._rules = rules_handler
        self._export = export_handler
        self._backup = backup_handler
        self._transcribe = transcribe_handler

    @property
    def runtime_state(self) -> TelegramCommandRuntimeState:
        return self._runtime_state

    @property
    def url_processor(self) -> Any:
        return self._runtime_state.url_processor

    @url_processor.setter
    def url_processor(self, value: Any) -> None:
        self._runtime_state.url_processor = value

    @property
    def url_handler(self) -> Any | None:
        return self._runtime_state.url_handler

    @url_handler.setter
    def url_handler(self, value: Any | None) -> None:
        self._runtime_state.url_handler = value

    @property
    def aggregation_handler(self) -> Any | None:
        return self._runtime_state.aggregation_handler

    @aggregation_handler.setter
    def aggregation_handler(self, value: Any | None) -> None:
        self._runtime_state.aggregation_handler = value

    @property
    def topic_searcher(self) -> Any | None:
        return self._runtime_state.topic_searcher

    @topic_searcher.setter
    def topic_searcher(self, value: Any | None) -> None:
        self._runtime_state.topic_searcher = value

    @property
    def local_searcher(self) -> Any | None:
        return self._runtime_state.local_searcher

    @local_searcher.setter
    def local_searcher(self, value: Any | None) -> None:
        self._runtime_state.local_searcher = value

    @property
    def hybrid_search(self) -> Any | None:
        return self._runtime_state.hybrid_search

    @hybrid_search.setter
    def hybrid_search(self, value: Any | None) -> None:
        self._runtime_state.hybrid_search = value

    @property
    def _task_manager(self) -> Any | None:
        return self._runtime_state._task_manager

    @_task_manager.setter
    def _task_manager(self, value: Any | None) -> None:
        self._runtime_state._task_manager = value

    async def dispatch_command(
        self,
        *,
        message: Any,
        text: str,
        uid: int,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> CommandDispatchOutcome:
        if not text.startswith("/"):
            return CommandDispatchOutcome(handled=False)

        if await dispatch_uid_routes(
            text,
            self._routes.pre_alias_uid,
            message=message,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        ):
            return CommandDispatchOutcome(handled=True)

        if await dispatch_text_routes(
            text,
            self._routes.pre_alias_text,
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        ):
            return CommandDispatchOutcome(handled=True)

        if await dispatch_alias_routes(
            text,
            self._routes.local_search_aliases,
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        ):
            return CommandDispatchOutcome(handled=True)

        if await dispatch_alias_routes(
            text,
            self._routes.online_search_aliases,
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        ):
            return CommandDispatchOutcome(handled=True)

        if await dispatch_text_routes(
            text,
            self._routes.pre_summarize_text,
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        ):
            return CommandDispatchOutcome(handled=True)

        summarize_outcome = await dispatch_summarize_fallback(
            text,
            summarize_prefix=self._routes.summarize_prefix,
            handler=self.handle_summarize_command,
            mark_awaiting_user=self.url_handler.add_awaiting_user if self.url_handler else None,
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        )
        if summarize_outcome.handled:
            return summarize_outcome

        if await dispatch_uid_routes(
            text,
            self._routes.post_summarize_uid,
            message=message,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        ):
            return CommandDispatchOutcome(handled=True)

        if await dispatch_text_routes(
            text,
            self._routes.post_summarize_text,
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        ):
            return CommandDispatchOutcome(handled=True)

        if await dispatch_uid_routes(
            text,
            self._routes.tail_uid,
            message=message,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        ):
            return CommandDispatchOutcome(handled=True)

        return CommandDispatchOutcome(handled=False)

    async def handle_summarize_command(
        self,
        message: Any,
        text: str,
        uid: int,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> tuple[str | None, bool]:
        ctx = self._context_factory.build(
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        )
        return await self._url_commands.handle_summarize(ctx)

    async def handle_aggregate_command(
        self,
        message: Any,
        text: str,
        uid: int,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> tuple[str | None, bool]:
        ctx = self._context_factory.build(
            message=message,
            text=text,
            uid=uid,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            start_time=start_time,
        )
        return await self._aggregation.handle_aggregate(ctx)

    async def handle_init_session_contact(self, message: Any) -> None:
        await self._init_session.handle_contact(message)

    async def handle_init_session_webapp(self, message: Any) -> None:
        await self._init_session.handle_web_app_data(message)

    def has_active_init_session(self, uid: int) -> bool:
        return self._init_session.has_active_session(uid)

    @staticmethod
    def _maybe_load_json(payload: Any) -> Any:
        return maybe_load_json(payload)

    @staticmethod
    def _parse_unread_arguments(text: str | None) -> tuple[int, str | None]:
        return ContentHandler.parse_unread_arguments(text)


__all__ = ["CommandDispatchOutcome", "TelegramCommandDispatcher"]
