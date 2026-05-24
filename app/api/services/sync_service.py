"""Sync service compatibility facade."""

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

    from app.api.models.requests import SyncApplyItem
    from app.api.models.responses import (
        DeltaSyncResponseData,
        FullSyncResponseData,
        SyncApplyItemResult,
        SyncApplyResponseData,
        SyncEntityEnvelope,
        SyncSessionData,
    )
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

    async def async_get_all_for_user(self, _user_id: int) -> list[dict[str, Any]]:
        return []

    async def async_get_summary_for_sync_apply(
        self, _summary_id: int, _user_id: int
    ) -> dict[str, Any] | None:
        return None

    async def async_apply_sync_change(self, *_args: Any, **_kwargs: Any) -> int:
        return 0


class _NullSyncAuxReadPort:
    async def get_highlights_for_user(self, _user_id: int) -> list[dict[str, Any]]:
        return []

    async def get_tags_for_user(self, _user_id: int) -> list[dict[str, Any]]:
        return []

    async def get_summary_tags_for_user(self, _user_id: int) -> list[dict[str, Any]]:
        return []


class SyncService:
    """Public sync service import path.

    SyncFacade is the authoritative coordinator for session, full, delta,
    apply, and idempotency behavior. This class keeps the long-standing
    ``app.api.services.sync_service.SyncService`` import stable for DI,
    routers, and tests while delegating protocol behavior to SyncFacade.
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
        self._user_repo = user_repository or _NullRepository()
        self._request_repo = request_repository or _NullRepository()
        self._summary_repo = summary_repository or _NullRepository()
        self._crawl_repo = crawl_result_repository or _NullRepository()
        self._llm_repo = llm_repository or _NullRepository()

        self._serializer = envelope_serializer or SyncEnvelopeSerializer()
        self._entity_adapters = tuple(entity_adapters) if entity_adapters is not None else None
        self._fallback_store = InMemorySyncSessionStore()
        self._session_store = session_store or FallbackSyncSessionStore(
            redis_store=RedisSyncSessionStore(
                cfg,
                get_redis_func=lambda current_cfg: get_redis(current_cfg),
            ),
            fallback_store=self._fallback_store,
        )
        self._aux_read_port = aux_read_port or _NullSyncAuxReadPort()
        self._collector = record_collector or SyncRecordCollector(
            user_repository=self._user_repo,
            request_repository=self._request_repo,
            summary_repository=self._summary_repo,
            crawl_result_repository=self._crawl_repo,
            llm_repository=self._llm_repo,
            aux_read_port=self._aux_read_port,
            serializer=self._serializer,
            adapters=self._entity_adapters,
        )
        self._apply_service = apply_service or SyncApplyService(
            user_repository=self._user_repo,
            request_repository=self._request_repo,
            summary_repository=self._summary_repo,
            crawl_result_repository=self._crawl_repo,
            llm_repository=self._llm_repo,
            aux_read_port=self._aux_read_port,
            serializer=self._serializer,
            adapters=self._entity_adapters,
        )
        self._facade = SyncFacade(
            cfg=cfg,
            session_store=self._session_store,
            collector=self._collector,
            apply_service=self._apply_service,
            user_repository=self._user_repo,
            request_repository=self._request_repo,
            summary_repository=self._summary_repo,
            crawl_result_repository=self._crawl_repo,
            llm_repository=self._llm_repo,
        )
        self._sync_sessions = self._fallback_store._sessions

    @property
    def _redis_warning_logged(self) -> bool:
        return getattr(self._session_store, "_redis_warning_logged", False)

    @_redis_warning_logged.setter
    def _redis_warning_logged(self, value: bool) -> None:
        if hasattr(self._session_store, "_redis_warning_logged"):
            self._session_store._redis_warning_logged = value

    async def get_max_server_version(self, user_id: int) -> int:
        return await self._facade.get_max_server_version(user_id)

    def _resolve_limit(self, requested: int | None) -> int:
        return self._facade._resolve_limit(requested)

    async def _store_session(self, payload: dict[str, Any]) -> None:
        await self._facade._store_session(payload)

    async def _load_session(
        self, session_id: str, user_id: int, client_id: str | None
    ) -> dict[str, Any]:
        return await self._facade._load_session(session_id, user_id, client_id)

    async def validate_session(
        self, session_id: str, user_id: int, client_id: str | None
    ) -> dict[str, Any]:
        return await self._facade.validate_session(session_id, user_id, client_id)

    async def start_session(
        self, *, user_id: int, client_id: str | None, limit: int | None
    ) -> SyncSessionData:
        return await self._facade.start_session(user_id=user_id, client_id=client_id, limit=limit)

    def _coerce_iso(self, dt_value: Any) -> str:
        return self._serializer._coerce_iso(dt_value)

    def _serialize_request(self, request: dict[str, Any]) -> SyncEntityEnvelope:
        return self._serializer.serialize_request(request)

    def _serialize_summary(self, summary: dict[str, Any]) -> SyncEntityEnvelope:
        return self._serializer.serialize_summary(summary)

    def _serialize_crawl_result(self, crawl: dict[str, Any]) -> SyncEntityEnvelope:
        return self._serializer.serialize_crawl_result(crawl)

    def _serialize_llm_call(self, call: dict[str, Any]) -> SyncEntityEnvelope:
        return self._serializer.serialize_llm_call(call)

    def _serialize_highlight(self, highlight: dict[str, Any]) -> SyncEntityEnvelope:
        return self._serializer.serialize_highlight(highlight)

    def _serialize_user(self, user: dict[str, Any]) -> SyncEntityEnvelope:
        return self._serializer.serialize_user(user)

    def _serialize_tag(self, tag: dict[str, Any]) -> SyncEntityEnvelope:
        return self._serializer.serialize_tag(tag)

    def _serialize_summary_tag(self, st: dict[str, Any]) -> SyncEntityEnvelope:
        return self._serializer.serialize_summary_tag(st)

    async def _get_highlights_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return await self._aux_read_port.get_highlights_for_user(user_id)

    async def _get_tags_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return await self._aux_read_port.get_tags_for_user(user_id)

    async def _get_summary_tags_for_user(self, user_id: int) -> list[dict[str, Any]]:
        return await self._aux_read_port.get_summary_tags_for_user(user_id)

    async def _collect_records(self, user_id: int) -> list[SyncEntityEnvelope]:
        return await self._collector.collect_records(user_id)

    def _paginate_records(
        self, records: Iterable[SyncEntityEnvelope], since: int, limit: int
    ) -> tuple[list[SyncEntityEnvelope], bool, int | None]:
        return self._collector.paginate_records(records, since, limit)

    async def get_full(
        self, *, session_id: str, user_id: int, client_id: str | None, limit: int | None
    ) -> FullSyncResponseData:
        return await self._facade.get_full(
            session_id=session_id,
            user_id=user_id,
            client_id=client_id,
            limit=limit,
        )

    async def get_delta(
        self, *, session_id: str, user_id: int, client_id: str | None, since: int, limit: int | None
    ) -> DeltaSyncResponseData:
        return await self._facade.get_delta(
            session_id=session_id,
            user_id=user_id,
            client_id=client_id,
            since=since,
            limit=limit,
        )

    def _build_full(
        self,
        session_id: str,
        records: list[SyncEntityEnvelope],
        has_more: bool,
        next_since: int | None,
        limit: int,
    ) -> FullSyncResponseData:
        return self._facade._build_full(session_id, records, has_more, next_since, limit)

    def _build_delta(
        self,
        session_id: str,
        since: int,
        records: list[SyncEntityEnvelope],
        has_more: bool,
        next_since: int | None,
        limit: int,
    ) -> DeltaSyncResponseData:
        return self._facade._build_delta(session_id, since, records, has_more, next_since, limit)

    async def apply_changes(
        self,
        *,
        session_id: str,
        user_id: int,
        client_id: str | None,
        changes: list[SyncApplyItem],
        idempotency_key: str | None = None,
    ) -> SyncApplyResponseData:
        return await self._facade.apply_changes(
            session_id=session_id,
            user_id=user_id,
            client_id=client_id,
            changes=changes,
            idempotency_key=idempotency_key,
        )

    def _lookup_apply_dedup_cache(
        self, session_id: str, idempotency_key: str | None
    ) -> SyncApplyResponseData | None:
        if not idempotency_key:
            return None
        return self._facade._lookup_apply_dedup_cache(session_id, idempotency_key)

    def _store_apply_dedup_cache(
        self,
        session_id: str,
        idempotency_key: str | None,
        response: SyncApplyResponseData,
    ) -> None:
        self._facade._store_apply_dedup_cache(session_id, idempotency_key, response)

    async def _apply_summary_change(
        self, change: SyncApplyItem, user_id: int
    ) -> SyncApplyItemResult:
        return await self._apply_service.apply_summary_change(change, user_id)
