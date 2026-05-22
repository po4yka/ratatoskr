"""Coordinator for sync flows."""

from __future__ import annotations

import uuid
import time as _time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from app.api.exceptions import (
    SyncSessionExpiredError,
    SyncSessionForbiddenError,
    SyncSessionNotFoundError,
)
from app.api.models.responses import (
    DeltaSyncResponseData,
    FullSyncResponseData,
    PaginationInfo,
    SyncApplyItemResult,
    SyncApplyResponseData,
    SyncSessionData,
)
from app.core.time_utils import UTC

if TYPE_CHECKING:
    from .apply import SyncApplyService
    from .collector import SyncRecordCollector
    from .session_store import SyncSessionStorePort


class SyncFacade:
    """Authoritative sync protocol coordinator.

    ``app.api.services.sync_service.SyncService`` is the stable public import
    path used by routers and older tests. This class owns session, full, delta,
    apply, and apply-idempotency behavior; the public wrapper delegates here.
    """

    def __init__(
        self,
        *,
        cfg: Any,
        session_store: SyncSessionStorePort,
        collector: SyncRecordCollector,
        apply_service: SyncApplyService,
        user_repository: Any,
        request_repository: Any,
        summary_repository: Any,
        crawl_result_repository: Any,
        llm_repository: Any,
    ) -> None:
        self.cfg = cfg
        self._session_store = session_store
        self._collector = collector
        self._apply_service = apply_service
        self._user_repo = user_repository
        self._request_repo = request_repository
        self._summary_repo = summary_repository
        self._crawl_repo = crawl_result_repository
        self._llm_repo = llm_repository
        self._apply_dedup_cache: dict[tuple[str, str], tuple[float, SyncApplyResponseData]] = {}
        self._apply_dedup_ttl_sec: float = 300.0

    async def get_max_server_version(self, user_id: int) -> int:
        import asyncio

        versions = await asyncio.gather(
            self._user_repo.async_get_max_server_version(user_id),
            self._request_repo.async_get_max_server_version(user_id),
            self._summary_repo.async_get_max_server_version(user_id),
            self._crawl_repo.async_get_max_server_version(user_id),
            self._llm_repo.async_get_max_server_version(user_id),
        )
        return cast("int", max((v for v in versions if v is not None), default=0))

    async def validate_session(
        self, session_id: str, user_id: int, client_id: str | None
    ) -> dict[str, Any]:
        return await self._load_session(session_id, user_id, client_id)

    def _resolve_limit(self, requested: int | None) -> int:
        return cast(
            "int",
            max(
                self.cfg.sync.min_limit,
                min(self.cfg.sync.max_limit, requested or self.cfg.sync.default_limit),
            ),
        )

    async def _store_session(self, payload: dict[str, Any]) -> None:
        ttl_seconds = int(self.cfg.sync.expiry_hours * 3600)
        await self._session_store.store(payload, ttl_seconds=ttl_seconds)

    async def _load_session(
        self, session_id: str, user_id: int, client_id: str | None
    ) -> dict[str, Any]:
        payload = await self._session_store.load(session_id)
        if not payload:
            raise SyncSessionNotFoundError(session_id)

        if payload.get("user_id") != user_id or payload.get("client_id") != client_id:
            raise SyncSessionForbiddenError()

        expires_raw = payload["expires_at"]
        expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        if datetime.now(UTC) >= expires_at:
            await self._session_store.delete(session_id)
            raise SyncSessionExpiredError(session_id)
        return payload

    async def start_session(
        self, *, user_id: int, client_id: str | None, limit: int | None
    ) -> SyncSessionData:
        resolved = self._resolve_limit(limit)
        session_id = f"sync-{uuid.uuid4().hex[:16]}"
        now = datetime.now(UTC)
        expires_at = now + timedelta(hours=self.cfg.sync.expiry_hours)
        payload = {
            "session_id": session_id,
            "user_id": user_id,
            "client_id": client_id,
            "chunk_limit": resolved,
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "next_since": 0,
        }
        await self._store_session(payload)
        return SyncSessionData(
            session_id=session_id,
            expires_at=str(payload["expires_at"]),
            default_limit=self.cfg.sync.default_limit,
            max_limit=self.cfg.sync.max_limit,
            last_issued_since=0,
        )

    async def get_full(
        self, *, session_id: str, user_id: int, client_id: str | None, limit: int | None
    ) -> FullSyncResponseData:
        session = await self._load_session(session_id, user_id, client_id)
        resolved_limit = self._resolve_limit(limit or session.get("chunk_limit"))
        records = await self._collector.collect_records(user_id)
        page, has_more, next_since = self._collector.paginate_records(
            records,
            since=0,
            limit=resolved_limit,
        )
        return self._build_full(session_id, page, has_more, next_since, resolved_limit)

    async def get_delta(
        self,
        *,
        session_id: str,
        user_id: int,
        client_id: str | None,
        since: int,
        limit: int | None,
    ) -> DeltaSyncResponseData:
        session = await self._load_session(session_id, user_id, client_id)
        resolved_limit = self._resolve_limit(limit or session.get("chunk_limit"))
        records = await self._collector.collect_records(user_id)
        page, has_more, next_since = self._collector.paginate_records(
            records,
            since=since,
            limit=resolved_limit,
        )
        return self._build_delta(session_id, since, page, has_more, next_since, resolved_limit)

    async def apply_changes(
        self,
        *,
        session_id: str,
        user_id: int,
        client_id: str | None,
        changes: list[Any],
        idempotency_key: str | None = None,
    ) -> SyncApplyResponseData:
        cache_hit = self._lookup_apply_dedup_cache(session_id, idempotency_key)
        if cache_hit is not None:
            return cache_hit

        await self._load_session(session_id, user_id, client_id)
        results: list[SyncApplyItemResult] = []
        for change in changes:
            if change.entity_type != "summary":
                results.append(
                    SyncApplyItemResult(
                        entity_type=change.entity_type,
                        id=change.id,
                        status="invalid",
                        error_code="UNSUPPORTED_ENTITY",
                    )
                )
                continue
            results.append(await self._apply_service.apply_summary_change(change, user_id))

        conflicts_list = [r for r in results if r.status == "conflict"]
        response = SyncApplyResponseData(
            session_id=session_id,
            results=results,
            conflicts=conflicts_list or None,
            has_more=None,
        )
        self._store_apply_dedup_cache(session_id, idempotency_key, response)
        return response

    def _build_full(
        self,
        session_id: str,
        records: list[Any],
        has_more: bool,
        next_since: int | None,
        limit: int,
    ) -> FullSyncResponseData:
        pagination = PaginationInfo(
            total=len(records),
            limit=limit,
            offset=0,
            has_more=has_more,
        )
        return FullSyncResponseData(
            session_id=session_id,
            has_more=has_more,
            next_since=next_since,
            items=records,
            pagination=pagination,
        )

    def _build_delta(
        self,
        session_id: str,
        since: int,
        records: list[Any],
        has_more: bool,
        next_since: int | None,
        limit: int,
    ) -> DeltaSyncResponseData:
        _ = limit
        created = [rec for rec in records if not rec.deleted_at]
        deleted = [rec for rec in records if rec.deleted_at]
        return DeltaSyncResponseData(
            session_id=session_id,
            since=since,
            has_more=has_more,
            next_since=next_since,
            created=created,
            updated=[],
            deleted=deleted,
        )

    def _lookup_apply_dedup_cache(
        self,
        session_id: str,
        idempotency_key: str | None,
    ) -> SyncApplyResponseData | None:
        if not idempotency_key:
            return None

        now = _time.monotonic()
        expired = [
            key
            for key, (cached_at, _) in self._apply_dedup_cache.items()
            if now - cached_at > self._apply_dedup_ttl_sec
        ]
        for key in expired:
            del self._apply_dedup_cache[key]

        entry = self._apply_dedup_cache.get((session_id, idempotency_key))
        if entry is None:
            return None
        return entry[1]

    def _store_apply_dedup_cache(
        self,
        session_id: str,
        idempotency_key: str | None,
        response: SyncApplyResponseData,
    ) -> None:
        if not idempotency_key:
            return
        self._apply_dedup_cache[(session_id, idempotency_key)] = (_time.monotonic(), response)
