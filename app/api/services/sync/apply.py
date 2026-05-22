"""Apply-side helpers for sync flows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.api.models.responses import SyncApplyItemResult

from .adapters import SyncEntityAdapter, SyncEntityAdapterContext, default_sync_entity_adapters

if TYPE_CHECKING:
    from .serializer import SyncEnvelopeSerializer


class SyncApplyService:
    def __init__(
        self,
        *,
        summary_repository: Any,
        serializer: SyncEnvelopeSerializer,
        user_repository: Any = None,
        request_repository: Any = None,
        crawl_result_repository: Any = None,
        llm_repository: Any = None,
        aux_read_port: Any = None,
        adapters: tuple[SyncEntityAdapter, ...] | None = None,
    ) -> None:
        self._summary_repo = summary_repository
        self._serializer = serializer
        self._adapters = {
            adapter.entity_type: adapter for adapter in adapters or default_sync_entity_adapters()
        }
        self._context = SyncEntityAdapterContext(
            user_repository=user_repository,
            request_repository=request_repository,
            summary_repository=self._summary_repo,
            crawl_result_repository=crawl_result_repository,
            llm_repository=llm_repository,
            aux_read_port=aux_read_port,
            serializer=self._serializer,
        )

    async def apply_change(self, change: Any, user_id: int) -> SyncApplyItemResult:
        adapter = self._adapters.get(change.entity_type)
        if adapter is None or adapter.apply_change is None:
            return SyncApplyItemResult(
                entity_type=change.entity_type,
                id=change.id,
                status="invalid",
                error_code="UNSUPPORTED_ENTITY",
            )
        return await adapter.apply(self._context, change, user_id)

    async def apply_summary_change(self, change: Any, user_id: int) -> SyncApplyItemResult:
        return await self.apply_change(change, user_id)
