"""Entity adapters for sync collection, serialization, and apply dispatch."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from app.api.models.responses import SyncApplyItemResult, SyncEntityEnvelope
from app.core.time_utils import UTC

from .serializer import SyncEnvelopeSerializer


@dataclass(slots=True)
class SyncEntityAdapterContext:
    user_repository: Any
    request_repository: Any
    summary_repository: Any
    crawl_result_repository: Any
    llm_repository: Any
    aux_read_port: Any
    serializer: SyncEnvelopeSerializer
    # Sync cursor for this collect pass. Carried on the context (not the collect
    # signature) so third-party adapters keep their (context, user_id) callable
    # shape. 0 => full read (first sync); >0 => only rows changed past the cursor,
    # pushed into each repo/aux query so a poll never re-reads a user's entire
    # lifetime history (audit #2).
    since: int = 0
    page_mode: bool = False
    limit: int | None = None
    through_version: int | None = None


async def _collect_page_projection(
    context: SyncEntityAdapterContext,
    entity_type: str,
    user_id: int,
) -> Iterable[dict[str, Any]] | None:
    """Read a bounded projection when the production page port is available."""
    if not context.page_mode:
        return None
    getter = getattr(type(context.aux_read_port), "get_sync_page", None)
    if getter is None:
        return None
    return cast(
        "Iterable[dict[str, Any]]",
        await getter(
            context.aux_read_port,
            entity_type,
            user_id,
            since=context.since,
            limit=context.limit,
            through_version=context.through_version,
        ),
    )


CollectSyncRecords = Callable[[SyncEntityAdapterContext, int], Awaitable[Iterable[dict[str, Any]]]]
SerializeSyncRecord = Callable[[SyncEnvelopeSerializer, dict[str, Any]], SyncEntityEnvelope]
MaxServerVersion = Callable[[SyncEntityAdapterContext, int], Awaitable[int | None]]
ApplySyncChange = Callable[[SyncEntityAdapterContext, Any, int], Awaitable[SyncApplyItemResult]]


@dataclass(frozen=True, slots=True)
class SyncEntityAdapter:
    entity_type: str
    collect_records: CollectSyncRecords
    serialize_record: SerializeSyncRecord
    max_server_version: MaxServerVersion | None = None
    apply_change: ApplySyncChange | None = None

    async def collect(
        self, context: SyncEntityAdapterContext, user_id: int
    ) -> Iterable[dict[str, Any]]:
        return await self.collect_records(context, user_id)

    def serialize(
        self, serializer: SyncEnvelopeSerializer, record: dict[str, Any]
    ) -> SyncEntityEnvelope:
        return self.serialize_record(serializer, record)

    async def get_max_server_version(
        self, context: SyncEntityAdapterContext, user_id: int
    ) -> int | None:
        if self.max_server_version is None:
            return None
        return await self.max_server_version(context, user_id)

    async def apply(
        self, context: SyncEntityAdapterContext, change: Any, user_id: int
    ) -> SyncApplyItemResult:
        if self.apply_change is None:
            return unsupported_entity_result(change)
        return await self.apply_change(context, change, user_id)


async def _collect_user(
    context: SyncEntityAdapterContext, user_id: int
) -> Iterable[dict[str, Any]]:
    projected = await _collect_page_projection(context, "user", user_id)
    if projected is not None:
        return projected
    user = await context.user_repository.async_get_user_by_telegram_id(user_id)
    return (user,) if user else ()


async def _collect_requests(
    context: SyncEntityAdapterContext, user_id: int
) -> Iterable[dict[str, Any]]:
    projected = await _collect_page_projection(context, "request", user_id)
    if projected is not None:
        return projected
    return cast(
        "Iterable[dict[str, Any]]",
        await context.request_repository.async_get_all_for_user(user_id, since=context.since),
    )


async def _collect_summaries(
    context: SyncEntityAdapterContext, user_id: int
) -> Iterable[dict[str, Any]]:
    projected = await _collect_page_projection(context, "summary", user_id)
    if projected is not None:
        return projected
    return cast(
        "Iterable[dict[str, Any]]",
        await context.summary_repository.async_get_all_for_user(user_id, since=context.since),
    )


async def _collect_crawl_results(
    context: SyncEntityAdapterContext, user_id: int
) -> Iterable[dict[str, Any]]:
    projected = await _collect_page_projection(context, "crawl_result", user_id)
    if projected is not None:
        return projected
    return cast(
        "Iterable[dict[str, Any]]",
        await context.crawl_result_repository.async_get_all_for_user(user_id, since=context.since),
    )


async def _collect_llm_calls(
    context: SyncEntityAdapterContext, user_id: int
) -> Iterable[dict[str, Any]]:
    projected = await _collect_page_projection(context, "llm_call", user_id)
    if projected is not None:
        return projected
    return cast(
        "Iterable[dict[str, Any]]",
        await context.llm_repository.async_get_all_for_user(user_id, since=context.since),
    )


async def _collect_highlights(
    context: SyncEntityAdapterContext, user_id: int
) -> Iterable[dict[str, Any]]:
    projected = await _collect_page_projection(context, "highlight", user_id)
    if projected is not None:
        return projected
    return cast(
        "Iterable[dict[str, Any]]",
        await context.aux_read_port.get_highlights_for_user(user_id, since=context.since),
    )


async def _collect_tags(
    context: SyncEntityAdapterContext, user_id: int
) -> Iterable[dict[str, Any]]:
    projected = await _collect_page_projection(context, "tag", user_id)
    if projected is not None:
        return projected
    return cast(
        "Iterable[dict[str, Any]]",
        await context.aux_read_port.get_tags_for_user(user_id, since=context.since),
    )


async def _collect_summary_tags(
    context: SyncEntityAdapterContext, user_id: int
) -> Iterable[dict[str, Any]]:
    projected = await _collect_page_projection(context, "summary_tag", user_id)
    if projected is not None:
        return projected
    return cast(
        "Iterable[dict[str, Any]]",
        await context.aux_read_port.get_summary_tags_for_user(user_id, since=context.since),
    )


async def _collect_stats(
    context: SyncEntityAdapterContext, user_id: int
) -> Iterable[dict[str, Any]]:
    getter = getattr(context.aux_read_port, "get_stats_for_user", None)
    if getter is None:
        return ()
    return cast("Iterable[dict[str, Any]]", await getter(user_id))


async def _max_user_version(context: SyncEntityAdapterContext, user_id: int) -> int | None:
    return cast("int | None", await context.user_repository.async_get_max_server_version(user_id))


async def _max_request_version(context: SyncEntityAdapterContext, user_id: int) -> int | None:
    return cast(
        "int | None",
        await context.request_repository.async_get_max_server_version(user_id),
    )


async def _max_summary_version(context: SyncEntityAdapterContext, user_id: int) -> int | None:
    return cast(
        "int | None",
        await context.summary_repository.async_get_max_server_version(user_id),
    )


async def _max_crawl_result_version(context: SyncEntityAdapterContext, user_id: int) -> int | None:
    return cast(
        "int | None",
        await context.crawl_result_repository.async_get_max_server_version(user_id),
    )


async def _max_llm_call_version(context: SyncEntityAdapterContext, user_id: int) -> int | None:
    return cast("int | None", await context.llm_repository.async_get_max_server_version(user_id))


async def _apply_summary_change(
    context: SyncEntityAdapterContext,
    change: Any,
    user_id: int,
) -> SyncApplyItemResult:
    try:
        summary_id = int(change.id)
    except (ValueError, TypeError):
        return SyncApplyItemResult(
            entity_type=change.entity_type,
            id=change.id,
            status="invalid",
            error_code="INVALID_ID",
        )

    summary = await context.summary_repository.async_get_summary_for_sync_apply(summary_id, user_id)
    if not summary:
        return SyncApplyItemResult(
            entity_type=change.entity_type,
            id=change.id,
            status="invalid",
            error_code="NOT_FOUND",
        )

    current_version = int(summary.get("server_version") or 0)
    if change.last_seen_version < current_version:
        snapshot = context.serializer.serialize_summary(summary)
        return SyncApplyItemResult(
            entity_type=change.entity_type,
            id=change.id,
            status="conflict",
            server_version=current_version,
            server_snapshot=snapshot,
            error_code="CONFLICT_VERSION",
        )

    payload = change.payload or {}
    allowed_fields = {"is_read", "is_favorited"}
    invalid_fields = [field for field in payload if field not in allowed_fields]
    if invalid_fields:
        return SyncApplyItemResult(
            entity_type=change.entity_type,
            id=change.id,
            status="invalid",
            error_code="INVALID_FIELDS",
            server_version=current_version,
        )

    is_deleted = None
    deleted_at = None
    is_read = None
    is_favorited = None
    if change.action == "delete":
        is_deleted = True
        deleted_at = datetime.now(UTC)
    else:
        if "is_read" in payload:
            is_read = bool(payload["is_read"])
        if "is_favorited" in payload:
            is_favorited = bool(payload["is_favorited"])

    new_version = await context.summary_repository.async_apply_sync_change(
        summary_id,
        user_id,
        is_deleted=is_deleted,
        deleted_at=deleted_at,
        is_read=is_read,
        is_favorited=is_favorited,
    )

    return SyncApplyItemResult(
        entity_type=change.entity_type,
        id=change.id,
        status="applied",
        server_version=new_version,
    )


def unsupported_entity_result(change: Any) -> SyncApplyItemResult:
    return SyncApplyItemResult(
        entity_type=change.entity_type,
        id=change.id,
        status="invalid",
        error_code="UNSUPPORTED_ENTITY",
    )


def default_sync_entity_adapters() -> tuple[SyncEntityAdapter, ...]:
    return (
        SyncEntityAdapter(
            entity_type="user",
            collect_records=_collect_user,
            serialize_record=lambda serializer, row: serializer.serialize_user(row),
            max_server_version=_max_user_version,
        ),
        SyncEntityAdapter(
            entity_type="request",
            collect_records=_collect_requests,
            serialize_record=lambda serializer, row: serializer.serialize_request(row),
            max_server_version=_max_request_version,
        ),
        SyncEntityAdapter(
            entity_type="summary",
            collect_records=_collect_summaries,
            serialize_record=lambda serializer, row: serializer.serialize_summary(row),
            max_server_version=_max_summary_version,
            apply_change=_apply_summary_change,
        ),
        SyncEntityAdapter(
            entity_type="crawl_result",
            collect_records=_collect_crawl_results,
            serialize_record=lambda serializer, row: serializer.serialize_crawl_result(row),
            max_server_version=_max_crawl_result_version,
        ),
        SyncEntityAdapter(
            entity_type="llm_call",
            collect_records=_collect_llm_calls,
            serialize_record=lambda serializer, row: serializer.serialize_llm_call(row),
            max_server_version=_max_llm_call_version,
        ),
        SyncEntityAdapter(
            entity_type="highlight",
            collect_records=_collect_highlights,
            serialize_record=lambda serializer, row: serializer.serialize_highlight(row),
        ),
        SyncEntityAdapter(
            entity_type="tag",
            collect_records=_collect_tags,
            serialize_record=lambda serializer, row: serializer.serialize_tag(row),
        ),
        SyncEntityAdapter(
            entity_type="summary_tag",
            collect_records=_collect_summary_tags,
            serialize_record=lambda serializer, row: serializer.serialize_summary_tag(row),
        ),
        SyncEntityAdapter(
            entity_type="stat",
            collect_records=_collect_stats,
            serialize_record=lambda serializer, row: serializer.serialize_stat(row),
        ),
    )
