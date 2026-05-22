"""SQLAlchemy implementation of aggregation session repository."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, update

from app.core.time_utils import UTC
from app.db.json_utils import prepare_json_payload
from app.db.models import (
    AggregationSession,
    AggregationSessionItem,
    CrawlResult,
    Request,
    Summary,
    model_to_dict,
)
from app.domain.models.source import AggregationItemStatus, AggregationSessionStatus, SourceItem

if TYPE_CHECKING:
    from app.application.dto.aggregation import AggregationFailure, NormalizedSourceDocument
    from app.db.session import Database


def _status_value(status: AggregationItemStatus | AggregationSessionStatus | str) -> str:
    return status.value if hasattr(status, "value") else str(status)


_TERMINAL_SESSION_STATUSES = {
    AggregationSessionStatus.COMPLETED.value,
    AggregationSessionStatus.PARTIAL.value,
    AggregationSessionStatus.FAILED.value,
    AggregationSessionStatus.CANCELLED.value,
}


def _progress_percent(
    *,
    total_items: int,
    successful_count: int,
    failed_count: int,
    duplicate_count: int,
) -> int:
    if total_items <= 0:
        return 0
    processed_items = min(total_items, successful_count + failed_count + duplicate_count)
    return min(100, int((processed_items * 100) / total_items))


class AggregationSessionRepositoryAdapter:
    """Adapter for aggregation bundle persistence operations."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_create_aggregation_session(
        self,
        user_id: int,
        correlation_id: str,
        total_items: int,
        *,
        allow_partial_success: bool = True,
        bundle_metadata: dict[str, Any] | None = None,
    ) -> int:
        now = datetime.now(UTC)
        async with self._database.transaction() as db_session:
            session = AggregationSession(
                user_id=user_id,
                correlation_id=correlation_id,
                total_items=total_items,
                allow_partial_success=allow_partial_success,
                bundle_metadata_json=prepare_json_payload(bundle_metadata),
                status=AggregationSessionStatus.PENDING.value,
                progress_percent=0,
                queued_at=now,
                last_progress_at=now,
            )
            db_session.add(session)
            await db_session.flush()
            return session.id

    async def async_get_aggregation_session(self, session_id: int) -> dict[str, Any] | None:
        async with self._database.session() as db_session:
            session = await db_session.get(AggregationSession, session_id)
            return _session_to_dict(session)

    async def async_get_aggregation_session_by_correlation_id(
        self, correlation_id: str
    ) -> dict[str, Any] | None:
        async with self._database.session() as db_session:
            session = await db_session.scalar(
                select(AggregationSession).where(
                    AggregationSession.correlation_id == correlation_id
                )
            )
            return _session_to_dict(session)

    async def async_get_user_aggregation_sessions(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._database.session() as db_session:
            stmt = (
                select(AggregationSession)
                .where(AggregationSession.user_id == user_id)
                .order_by(AggregationSession.created_at.desc())
            )
            if status:
                stmt = stmt.where(AggregationSession.status == status)
            sessions = (await db_session.execute(stmt.limit(limit).offset(offset))).scalars()
            return [_session_to_dict(session) or {} for session in sessions]

    async def async_count_user_aggregation_sessions(
        self,
        user_id: int,
        *,
        status: str | None = None,
    ) -> int:
        async with self._database.session() as db_session:
            stmt = select(func.count(AggregationSession.id)).where(
                AggregationSession.user_id == user_id
            )
            if status:
                stmt = stmt.where(AggregationSession.status == status)
            return int(await db_session.scalar(stmt) or 0)

    async def async_add_aggregation_session_item(
        self,
        session_id: int,
        source_item: SourceItem,
        position: int,
        *,
        request_id: int | None = None,
    ) -> int:
        async with self._database.transaction() as db_session:
            first_match_id = await db_session.scalar(
                select(AggregationSessionItem.id)
                .where(
                    AggregationSessionItem.aggregation_session_id == session_id,
                    AggregationSessionItem.source_item_id == source_item.stable_id,
                    AggregationSessionItem.duplicate_of_item_id.is_(None),
                )
                .order_by(AggregationSessionItem.position)
            )
            item = AggregationSessionItem(
                aggregation_session_id=session_id,
                request_id=request_id,
                position=position,
                source_kind=source_item.kind.value,
                source_item_id=source_item.stable_id,
                source_dedupe_key=source_item.dedupe_key,
                original_value=source_item.original_value,
                normalized_value=source_item.normalized_value,
                external_id=source_item.external_id,
                telegram_chat_id=source_item.telegram_chat_id,
                telegram_message_id=source_item.telegram_message_id,
                telegram_media_group_id=source_item.telegram_media_group_id,
                title_hint=source_item.title_hint,
                source_metadata_json=prepare_json_payload(source_item.metadata),
                status=(
                    AggregationItemStatus.DUPLICATE.value
                    if first_match_id is not None
                    else AggregationItemStatus.PENDING.value
                ),
                duplicate_of_item_id=first_match_id,
            )
            db_session.add(item)
            await db_session.flush()
            return item.id

    async def async_get_aggregation_session_items(self, session_id: int) -> list[dict[str, Any]]:
        async with self._database.session() as db_session:
            rows = (
                await db_session.execute(
                    select(
                        AggregationSessionItem,
                        CrawlResult.id.label("crawl_result_id"),
                        Summary.id.label("summary_id"),
                        Request.is_deleted.label("request_is_deleted"),
                        Summary.is_deleted.label("summary_is_deleted"),
                    )
                    .outerjoin(Request, AggregationSessionItem.request_id == Request.id)
                    .outerjoin(CrawlResult, CrawlResult.request_id == Request.id)
                    .outerjoin(Summary, Summary.request_id == Request.id)
                    .where(AggregationSessionItem.aggregation_session_id == session_id)
                    .order_by(AggregationSessionItem.position)
                )
            ).all()
            return [
                _item_to_dict(
                    item,
                    crawl_result_id=crawl_result_id,
                    summary_id=summary_id,
                    request_is_deleted=bool(request_is_deleted),
                    summary_is_deleted=bool(summary_is_deleted),
                )
                or {}
                for (
                    item,
                    crawl_result_id,
                    summary_id,
                    request_is_deleted,
                    summary_is_deleted,
                ) in rows
            ]

    async def async_update_aggregation_session_item_result(
        self,
        item_id: int,
        *,
        status: AggregationItemStatus | str,
        request_id: int | None = None,
        normalized_document: NormalizedSourceDocument | None = None,
        extraction_metadata: dict[str, Any] | None = None,
        failure: AggregationFailure | None = None,
    ) -> None:
        update_fields: dict[str, Any] = {
            "status": _status_value(status),
            "updated_at": datetime.now(UTC),
        }
        if request_id is not None:
            update_fields["request_id"] = request_id
        if normalized_document is not None:
            update_fields["normalized_document_json"] = prepare_json_payload(
                normalized_document.model_dump(mode="json")
            )
        if extraction_metadata is not None:
            update_fields["extraction_metadata_json"] = prepare_json_payload(extraction_metadata)
        if failure is not None:
            update_fields["failure_code"] = failure.code
            update_fields["failure_message"] = failure.message
            update_fields["failure_details_json"] = prepare_json_payload(failure.details)
        elif _status_value(status) != AggregationItemStatus.FAILED.value:
            update_fields["failure_code"] = None
            update_fields["failure_message"] = None
            update_fields["failure_details_json"] = None

        async with self._database.transaction() as db_session:
            await db_session.execute(
                update(AggregationSessionItem)
                .where(AggregationSessionItem.id == item_id)
                .values(**update_fields)
            )

    async def async_update_aggregation_session_counts(
        self,
        session_id: int,
        *,
        successful_count: int,
        failed_count: int,
        duplicate_count: int,
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.transaction() as db_session:
            total_items = await db_session.scalar(
                select(AggregationSession.total_items).where(AggregationSession.id == session_id)
            )
            await db_session.execute(
                update(AggregationSession)
                .where(AggregationSession.id == session_id)
                .values(
                    successful_count=successful_count,
                    failed_count=failed_count,
                    duplicate_count=duplicate_count,
                    progress_percent=_progress_percent(
                        total_items=int(total_items or 0),
                        successful_count=successful_count,
                        failed_count=failed_count,
                        duplicate_count=duplicate_count,
                    ),
                    last_progress_at=now,
                    updated_at=now,
                )
            )

    async def async_update_aggregation_session_output(
        self,
        session_id: int,
        aggregation_output: dict[str, Any],
    ) -> None:
        now = datetime.now(UTC)
        async with self._database.transaction() as db_session:
            await db_session.execute(
                update(AggregationSession)
                .where(AggregationSession.id == session_id)
                .values(
                    aggregation_output_json=prepare_json_payload(aggregation_output),
                    last_progress_at=now,
                    updated_at=now,
                )
            )

    async def async_update_aggregation_session_status(
        self,
        session_id: int,
        *,
        status: AggregationSessionStatus | str,
        processing_time_ms: int | None = None,
        failure: AggregationFailure | None = None,
    ) -> None:
        now = datetime.now(UTC)
        status_value = _status_value(status)
        update_fields: dict[str, Any] = {
            "status": status_value,
            "last_progress_at": now,
            "updated_at": now,
        }
        if status_value != AggregationSessionStatus.PENDING.value:
            update_fields["started_at"] = now
        if processing_time_ms is not None:
            update_fields["processing_time_ms"] = processing_time_ms
        if failure is not None:
            update_fields["failure_code"] = failure.code
            update_fields["failure_message"] = failure.message
            update_fields["failure_details_json"] = prepare_json_payload(failure.details)
        elif status_value != AggregationSessionStatus.FAILED.value:
            update_fields["failure_code"] = None
            update_fields["failure_message"] = None
            update_fields["failure_details_json"] = None
        if status_value in _TERMINAL_SESSION_STATUSES:
            update_fields["completed_at"] = now

        async with self._database.transaction() as db_session:
            await db_session.execute(
                update(AggregationSession)
                .where(AggregationSession.id == session_id)
                .values(**update_fields)
            )


def _session_to_dict(session: AggregationSession | None) -> dict[str, Any] | None:
    data = model_to_dict(session)
    if data is not None:
        data["user"] = data.get("user_id")
    return data


def _item_to_dict(
    item: AggregationSessionItem | None,
    *,
    crawl_result_id: int | None = None,
    summary_id: int | None = None,
    request_is_deleted: bool = False,
    summary_is_deleted: bool = False,
) -> dict[str, Any] | None:
    data = model_to_dict(item)
    if data is not None:
        data["aggregation_session"] = data.get("aggregation_session_id")
        data["request"] = data.get("request_id")
        data["crawl_result_id"] = crawl_result_id
        data["summary_id"] = summary_id
        data["request_is_deleted"] = request_is_deleted
        data["summary_is_deleted"] = summary_is_deleted
    return data
