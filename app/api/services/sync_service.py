"""Sync service - public entry point that constructs and exposes SyncFacade."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.api.services.sync import (
    FallbackSyncSessionStore,
    InMemorySyncSessionStore,
    RedisSyncSessionStore,
    SyncApplyService,
    SyncEnvelopeSerializer,
    SyncFacade,
    SyncRecordCollector,
)
from app.application.ports.requests import (  # noqa: TC001  # used at runtime in __init__ signature
    CrawlResultRepositoryPort,
    LLMRepositoryPort,
    RequestRepositoryPort,
)
from app.application.ports.summaries import (  # noqa: TC001  # used at runtime in __init__ signature
    SummaryRepositoryPort,
)
from app.application.ports.users import (  # noqa: TC001  # used at runtime in __init__ signature
    UserRepositoryPort,
)
from app.config import AppConfig  # noqa: TC001  # used at runtime in __init__ signature
from app.db.session import Database  # noqa: TC001  # used at runtime in __init__ signature
from app.infrastructure.redis import get_redis

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.api.services.sync.adapters import SyncEntityAdapter
    from app.api.services.sync.collector import SyncAuxReadPort
    from app.api.services.sync.session_store import SyncSessionStorePort

else:

    class SyncAuxReadPort:  # pragma: no cover - runtime fallback for typing only
        pass


class _NullRepository:
    async def async_get_max_server_version(self, _user_id: int) -> int:
        return 0

    async def async_get_user_by_telegram_id(self, _user_id: int) -> dict[str, Any] | None:
        return None

    async def async_get_all_for_user(
        self, _user_id: int, *, since: int = 0
    ) -> list[dict[str, Any]]:
        return []

    async def async_get_summary_for_sync_apply(
        self, _summary_id: int, _user_id: int
    ) -> dict[str, Any] | None:
        return None

    async def async_apply_sync_change(self, _summary_id: int, _user_id: int, **_kwargs: Any) -> int:
        return 0


class _NullSyncAuxReadPort:
    async def get_highlights_for_user(
        self, _user_id: int, *, since: int = 0
    ) -> list[dict[str, Any]]:
        return []

    async def get_tags_for_user(self, _user_id: int, *, since: int = 0) -> list[dict[str, Any]]:
        return []

    async def get_summary_tags_for_user(
        self, _user_id: int, *, since: int = 0
    ) -> list[dict[str, Any]]:
        return []


class SyncService(SyncFacade):
    """Public sync service that assembles dependencies and delegates to SyncFacade.

    Routers and tests use this class as the stable public import path
    (``app.api.services.sync_service.SyncService``).  All protocol behavior
    lives in ``SyncFacade``; this class only performs dependency wiring.
    """

    def __init__(
        self,
        cfg: AppConfig,
        session_manager: Database,
        *,
        user_repository: UserRepositoryPort | None = None,
        request_repository: RequestRepositoryPort | None = None,
        summary_repository: SummaryRepositoryPort | None = None,
        crawl_result_repository: CrawlResultRepositoryPort | None = None,
        llm_repository: LLMRepositoryPort | None = None,
        session_store: SyncSessionStorePort | None = None,
        aux_read_port: SyncAuxReadPort | None = None,
        record_collector: SyncRecordCollector | None = None,
        envelope_serializer: SyncEnvelopeSerializer | None = None,
        apply_service: SyncApplyService | None = None,
        entity_adapters: Iterable[SyncEntityAdapter] | None = None,
    ) -> None:
        self.cfg = cfg
        self._session_manager = session_manager

        user_repo = user_repository or _NullRepository()
        request_repo = request_repository or _NullRepository()
        summary_repo = summary_repository or _NullRepository()
        crawl_repo = crawl_result_repository or _NullRepository()
        llm_repo = llm_repository or _NullRepository()

        serializer = envelope_serializer or SyncEnvelopeSerializer()
        entity_adapters_tuple = tuple(entity_adapters) if entity_adapters is not None else None
        _fallback_store = InMemorySyncSessionStore()
        resolved_session_store = session_store or FallbackSyncSessionStore(
            redis_store=RedisSyncSessionStore(
                cfg,
                get_redis_func=lambda current_cfg: get_redis(current_cfg),
            ),
            fallback_store=_fallback_store,
        )
        resolved_aux_read_port = aux_read_port or _NullSyncAuxReadPort()
        resolved_collector = record_collector or SyncRecordCollector(
            user_repository=user_repo,
            request_repository=request_repo,
            summary_repository=summary_repo,
            crawl_result_repository=crawl_repo,
            llm_repository=llm_repo,
            aux_read_port=resolved_aux_read_port,
            serializer=serializer,
            adapters=entity_adapters_tuple,
        )
        resolved_apply_service = apply_service or SyncApplyService(
            user_repository=user_repo,
            request_repository=request_repo,
            summary_repository=summary_repo,
            crawl_result_repository=crawl_repo,
            llm_repository=llm_repo,
            aux_read_port=resolved_aux_read_port,
            serializer=serializer,
            adapters=entity_adapters_tuple,
        )

        super().__init__(
            cfg=cfg,
            session_store=resolved_session_store,
            collector=resolved_collector,
            apply_service=resolved_apply_service,
            user_repository=user_repo,
            request_repository=request_repo,
            summary_repository=summary_repo,
            crawl_result_repository=crawl_repo,
            llm_repository=llm_repo,
        )

        # Expose serializer for tests that validate serialization behaviour.
        self._serializer = serializer
        # Expose the in-memory fallback session dict for tests that inspect it.
        self._sync_sessions = _fallback_store._sessions

    @property
    def _redis_warning_logged(self) -> bool:
        return getattr(self._session_store, "_redis_warning_logged", False)

    @_redis_warning_logged.setter
    def _redis_warning_logged(self, value: bool) -> None:
        if hasattr(self._session_store, "_redis_warning_logged"):
            self._session_store._redis_warning_logged = value
