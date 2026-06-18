"""Record collection helpers for sync flows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from .adapters import SyncEntityAdapter, SyncEntityAdapterContext, default_sync_entity_adapters

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.api.models.responses import SyncEntityEnvelope

    from .serializer import SyncEnvelopeSerializer


class SyncAuxReadPort(Protocol):
    async def get_highlights_for_user(self, user_id: int) -> list[dict[str, Any]]: ...

    async def get_tags_for_user(self, user_id: int) -> list[dict[str, Any]]: ...

    async def get_summary_tags_for_user(self, user_id: int) -> list[dict[str, Any]]: ...


class SyncRecordCollector:
    def __init__(
        self,
        *,
        user_repository: Any,
        request_repository: Any,
        summary_repository: Any,
        crawl_result_repository: Any,
        llm_repository: Any,
        aux_read_port: SyncAuxReadPort,
        serializer: SyncEnvelopeSerializer,
        adapters: Iterable[SyncEntityAdapter] | None = None,
    ) -> None:
        self._user_repo = user_repository
        self._request_repo = request_repository
        self._summary_repo = summary_repository
        self._crawl_repo = crawl_result_repository
        self._llm_repo = llm_repository
        self._aux_read_port = aux_read_port
        self._serializer = serializer
        self._adapters = tuple(adapters or default_sync_entity_adapters())
        self._context = SyncEntityAdapterContext(
            user_repository=self._user_repo,
            request_repository=self._request_repo,
            summary_repository=self._summary_repo,
            crawl_result_repository=self._crawl_repo,
            llm_repository=self._llm_repo,
            aux_read_port=self._aux_read_port,
            serializer=self._serializer,
        )

    async def collect_records(self, user_id: int) -> list[SyncEntityEnvelope]:
        records: list[SyncEntityEnvelope] = []

        for adapter in self._adapters:
            for record in await adapter.collect(self._context, user_id):
                records.append(adapter.serialize(self._serializer, record))

        records.sort(key=lambda r: (r.server_version, str(r.id)))
        return records

    async def get_max_server_version(self, user_id: int) -> int:
        versions = [
            version
            for adapter in self._adapters
            if (version := await adapter.get_max_server_version(self._context, user_id)) is not None
        ]
        return max(versions, default=0)

    @staticmethod
    def paginate_records(
        records: Iterable[SyncEntityEnvelope], since: int, limit: int
    ) -> tuple[list[SyncEntityEnvelope], bool, int | None]:
        filtered = [rec for rec in records if rec.server_version > since]
        if len(filtered) <= limit:
            page = filtered
            has_more = False
        else:
            boundary_version = filtered[limit - 1].server_version
            page = [rec for rec in filtered if rec.server_version <= boundary_version]
            has_more = len(page) < len(filtered)
        next_since = page[-1].server_version if page else since
        return page, has_more, next_since
