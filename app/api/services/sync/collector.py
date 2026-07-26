"""Record collection helpers for sync flows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from .adapters import SyncEntityAdapter, SyncEntityAdapterContext, default_sync_entity_adapters

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.api.models.responses import SyncEntityEnvelope

    from .serializer import SyncEnvelopeSerializer


def _created_at_ms(record: dict[str, Any]) -> int | None:
    """Return a record's created_at as epoch-ms, matching server_version units.

    server_version is ``int(created_at.timestamp() * 1000)`` at creation
    (``app.db.types._next_server_version``), so the delta bucketer can compare a
    created_at-ms against the ``since`` cursor (also a server_version) directly.
    Returns ``None`` when the record has no parseable created_at.
    """
    raw = record.get("created_at")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(raw, datetime):
        return int(raw.timestamp() * 1000)
    return None


class SyncAuxReadPort(Protocol):
    async def get_highlights_for_user(
        self, user_id: int, *, since: int = 0
    ) -> list[dict[str, Any]]: ...

    async def get_tags_for_user(self, user_id: int, *, since: int = 0) -> list[dict[str, Any]]: ...

    async def get_summary_tags_for_user(
        self, user_id: int, *, since: int = 0
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class SyncCollectedPage:
    records: list[SyncEntityEnvelope]
    has_more: bool
    next_since: int


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

    async def collect_records(self, user_id: int, since: int = 0) -> list[SyncEntityEnvelope]:
        records: list[SyncEntityEnvelope] = []

        # Carry the sync cursor on a per-call context copy so each adapter's repo /
        # aux query filters server_version > since at the DB (audit #2). since=0
        # (first sync) keeps the full-read behavior. The base self._context is never
        # mutated, so concurrent collects never see each other's cursor.
        context = replace(self._context, since=since) if since else self._context

        for adapter in self._adapters:
            for record in await adapter.collect(context, user_id):
                envelope = adapter.serialize(self._serializer, record)
                # Carry creation time so delta sync can tell a new row from an
                # in-place edit; harmless for full sync, which ignores it.
                envelope._created_at_ms = _created_at_ms(record)
                records.append(envelope)

        records.sort(key=lambda r: (r.server_version, str(r.id)))
        return records

    async def collect_page(
        self,
        user_id: int,
        *,
        since: int,
        limit: int,
    ) -> SyncCollectedPage:
        """Collect one globally ordered page from bounded per-entity SQL heads.

        The wire cursor contains only ``server_version``, so every record tied
        at the page boundary must stay atomic. Most pages need one bounded query
        per entity. A second query is issued only for an entity whose head was
        truncated inside the boundary-version group; a one-row probe then
        determines whether a later version exists.
        """
        head_limit = max(1, limit) + 1
        head_context = replace(
            self._context,
            since=since,
            page_mode=True,
            limit=head_limit,
            through_version=None,
        )
        head_batches = await asyncio.gather(
            *(self._collect_adapter(adapter, head_context, user_id) for adapter in self._adapters)
        )
        head_records = self._sort_records(record for batch in head_batches for record in batch)
        if len(head_records) <= limit:
            next_since = head_records[-1].server_version if head_records else since
            return SyncCollectedPage(head_records, False, next_since)

        boundary_version = head_records[limit - 1].server_version
        page_batches: list[list[SyncEntityEnvelope]] = []
        truncated_indexes: list[int] = []
        for index, batch in enumerate(head_batches):
            if len(batch) == head_limit and batch and batch[-1].server_version <= boundary_version:
                truncated_indexes.append(index)
                page_batches.append([])
            else:
                page_batches.append(
                    [record for record in batch if record.server_version <= boundary_version]
                )

        if truncated_indexes:
            boundary_context = replace(
                self._context,
                since=since,
                page_mode=True,
                limit=None,
                through_version=boundary_version,
            )
            refetched = await asyncio.gather(
                *(
                    self._collect_adapter(self._adapters[index], boundary_context, user_id)
                    for index in truncated_indexes
                )
            )
            for index, batch in zip(truncated_indexes, refetched, strict=True):
                page_batches[index] = batch

        page = self._sort_records(record for batch in page_batches for record in batch)
        has_more = any(record.server_version > boundary_version for record in head_records)
        if not has_more and truncated_indexes:
            probe_context = replace(
                self._context,
                since=boundary_version,
                page_mode=True,
                limit=1,
                through_version=None,
            )
            probes = await asyncio.gather(
                *(
                    self._collect_adapter(self._adapters[index], probe_context, user_id)
                    for index in truncated_indexes
                )
            )
            has_more = any(probes)

        return SyncCollectedPage(page, has_more, boundary_version)

    async def _collect_adapter(
        self,
        adapter: SyncEntityAdapter,
        context: SyncEntityAdapterContext,
        user_id: int,
    ) -> list[SyncEntityEnvelope]:
        envelopes: list[SyncEntityEnvelope] = []
        for record in await adapter.collect(context, user_id):
            envelope = adapter.serialize(self._serializer, record)
            if envelope.server_version <= context.since:
                continue
            if (
                context.through_version is not None
                and envelope.server_version > context.through_version
            ):
                continue
            envelope._created_at_ms = _created_at_ms(record)
            envelopes.append(envelope)
        envelopes.sort(key=lambda record: (record.server_version, str(record.id)))
        if context.limit is not None:
            return envelopes[: context.limit]
        return envelopes

    @staticmethod
    def _sort_records(records: Iterable[SyncEntityEnvelope]) -> list[SyncEntityEnvelope]:
        return sorted(records, key=lambda record: (record.server_version, str(record.id)))

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
