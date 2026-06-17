"""SQLAlchemy implementation of the summary repository."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.application.services.topic_search_utils import ensure_mapping, tokenize
from app.core.logging_utils import get_logger
from app.core.time_utils import coerce_datetime
from app.db.json_utils import prepare_json_payload
from app.db.models import (
    AggregationSession,
    AggregationSessionItem,
    CrawlResult,
    Request,
    Summary,
    SummaryFeedback,
    model_to_dict,
)
from app.db.types import _next_server_version, _utcnow
from app.domain.models.request import RequestStatus
from app.domain.models.summary import Summary as DomainSummary

if TYPE_CHECKING:
    from datetime import datetime

    from app.db.session import Database

logger = get_logger(__name__)


class SummaryRepositoryAdapter:
    """Adapter for summary persistence using SQLAlchemy."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_upsert_summary(
        self,
        request_id: int,
        lang: str,
        json_payload: dict[str, Any],
        insights_json: dict[str, Any] | None = None,
        is_read: bool = False,
    ) -> int:
        """Create or update a summary and return its version."""
        return await self._upsert_summary_record(
            request_id=request_id,
            lang=lang,
            json_payload=json_payload,
            insights_json=insights_json,
            is_read=is_read,
        )

    async def async_finalize_request_summary(
        self,
        request_id: int,
        lang: str,
        json_payload: dict[str, Any],
        insights_json: dict[str, Any] | None = None,
        is_read: bool = False,
        request_status: RequestStatus = RequestStatus.COMPLETED,
    ) -> int:
        """Persist a summary and update request status in one transaction."""
        async with self._database.transaction() as session:
            version = await self._upsert_summary_record(
                request_id=request_id,
                lang=lang,
                json_payload=json_payload,
                insights_json=insights_json,
                is_read=is_read,
                session=session,
            )
            await session.execute(
                update(Request)
                .where(Request.id == request_id)
                .values(status=_status_value(request_status), updated_at=_utcnow())
            )
            return version

    async def async_update_summary_insights(
        self, request_id: int, insights_json: dict[str, Any]
    ) -> None:
        """Update the insights field of a summary."""
        async with self._database.transaction() as session:
            await session.execute(
                update(Summary)
                .where(Summary.request_id == request_id)
                .values(insights_json=prepare_json_payload(insights_json), updated_at=_utcnow())
            )

    async def async_get_user_summaries(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
        is_read: bool | None = None,
        is_favorited: bool | None = None,
        lang: str | None = None,
        start_date: Any | None = None,
        end_date: Any | None = None,
        sort: str = "created_at_desc",
        search: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Get paginated summaries for a user with filtering and stats."""
        async with self._database.session() as session:
            base_conditions = [Request.user_id == user_id, Summary.is_deleted.is_(False)]
            optional_conditions: list[Any] = []
            if is_read is not None:
                optional_conditions.append(Summary.is_read.is_(is_read))
            if is_favorited is not None:
                optional_conditions.append(Summary.is_favorited.is_(is_favorited))
            if lang:
                optional_conditions.append(Summary.lang == lang)
            if start_date:
                optional_conditions.append(Summary.created_at >= start_date)
            if end_date:
                optional_conditions.append(Summary.created_at <= end_date)
            if search:
                # Case-insensitive substring match on the article URL.
                # The Summary's title lives in Summary.json_payload (JSONB)
                # and needs a JSON-extract expression — defer that to a
                # follow-up. URL match covers the URL-paste search path.
                optional_conditions.append(Request.input_url.ilike(f"%{search}%"))
            conditions = [*base_conditions, *optional_conditions]

            # Single round-trip for both counts: `total` matches the active
            # filters, `unread_count` is the user's unread total over non-deleted
            # summaries (independent of the list filters), via FILTER aggregates.
            total_count = (
                func.count().filter(and_(*optional_conditions))
                if optional_conditions
                else func.count()
            )
            counts_row = (
                await session.execute(
                    select(total_count, func.count().filter(Summary.is_read.is_(False)))
                    .select_from(Summary)
                    .join(Request, Summary.request_id == Request.id)
                    .where(*base_conditions)
                )
            ).one()
            total = int(counts_row[0] or 0)
            unread_count = int(counts_row[1] or 0)

            order_by = Request.created_at.desc()
            if sort != "created_at_desc":
                order_by = Request.created_at.asc()
            rows = await session.execute(
                select(Summary, Request)
                .join(Request, Summary.request_id == Request.id)
                .where(*conditions)
                .order_by(order_by)
                .limit(limit)
                .offset(offset)
            )
            summaries: list[dict[str, Any]] = []
            for summary, request in rows:
                data = model_to_dict(summary) or {}
                data["request"] = model_to_dict(request)
                summaries.append(data)
            return summaries, total, unread_count

    async def async_get_summary_by_request(self, request_id: int) -> dict[str, Any] | None:
        """Get a summary by request ID."""
        async with self._database.session() as session:
            summary = await session.scalar(select(Summary).where(Summary.request_id == request_id))
            return model_to_dict(summary)

    async def async_get_summary_id_by_request(self, request_id: int) -> int | None:
        """Get a summary ID by its request ID."""
        async with self._database.session() as session:
            return await session.scalar(select(Summary.id).where(Summary.request_id == request_id))

    async def async_get_summary_by_id(self, summary_id: int) -> dict[str, Any] | None:
        """Get a summary by its ID."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(Summary, Request)
                    .join(Request, Summary.request_id == Request.id)
                    .where(Summary.id == summary_id)
                )
            ).first()
            if row is None:
                return None
            summary, request = row
            data = model_to_dict(summary) or {}
            data["user_id"] = request.user_id
            return data

    async def async_get_summary_context_by_id(self, summary_id: int) -> dict[str, Any] | None:
        """Get a summary with its request and crawl result in a single read."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(Summary, Request, CrawlResult)
                    .join(Request, Summary.request_id == Request.id)
                    .outerjoin(CrawlResult, CrawlResult.request_id == Request.id)
                    .where(Summary.id == summary_id)
                )
            ).first()
            if row is None:
                return None
            summary, request, crawl_result = row
            summary_data = model_to_dict(summary) or {}
            summary_data["user_id"] = request.user_id
            return {
                "summary": summary_data,
                "request": model_to_dict(request),
                "crawl_result": model_to_dict(crawl_result),
            }

    async def async_get_aggregation_source_bundle_for_summary(
        self, summary_id: int
    ) -> dict[str, Any] | None:
        """Return the newest aggregation session containing this summary's source request."""
        async with self._database.session() as session:
            session_id = await session.scalar(
                select(AggregationSession.id)
                .join(
                    AggregationSessionItem,
                    AggregationSessionItem.aggregation_session_id == AggregationSession.id,
                )
                .join(Request, AggregationSessionItem.request_id == Request.id)
                .join(Summary, Summary.request_id == Request.id)
                .where(Summary.id == summary_id)
                .order_by(AggregationSession.created_at.desc(), AggregationSession.id.desc())
                .limit(1)
            )
            if session_id is None:
                return None

            aggregation_session = await session.get(AggregationSession, session_id)
            rows = (
                await session.execute(
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
            return {
                "session": model_to_dict(aggregation_session),
                "items": [
                    _aggregation_item_to_dict(
                        item,
                        crawl_result_id=crawl_result_id,
                        summary_id=row_summary_id,
                        request_is_deleted=bool(request_is_deleted),
                        summary_is_deleted=bool(summary_is_deleted),
                    )
                    or {}
                    for (
                        item,
                        crawl_result_id,
                        row_summary_id,
                        request_is_deleted,
                        summary_is_deleted,
                    ) in rows
                ],
            }

    async def async_get_summaries_by_request_ids(
        self, request_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Get summaries by their request IDs."""
        if not request_ids:
            return {}
        async with self._database.session() as session:
            rows = (
                await session.execute(select(Summary).where(Summary.request_id.in_(request_ids)))
            ).scalars()
            return {row.request_id: model_to_dict(row) or {} for row in rows}

    async def async_get_unread_summaries(
        self,
        user_id: int | None,
        chat_id: int | None,
        limit: int = 10,
        topic: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get unread summaries for a user."""
        if limit <= 0:
            return []

        topic_query = topic.strip() if topic else None
        candidate_request_ids: list[int] | None = None
        fetch_limit: int | None = limit
        if topic_query:
            candidate_limit = max(limit * 5, 25)
            candidate_request_ids = await self._find_topic_search_request_ids(
                topic_query, candidate_limit=candidate_limit
            )
            fetch_limit = len(candidate_request_ids) if candidate_request_ids else None

        async with self._database.session() as session:
            conditions: list[Any] = [Summary.is_read.is_(False)]
            if user_id is not None:
                conditions.append(or_(Request.user_id == user_id, Request.user_id.is_(None)))
            if chat_id is not None:
                conditions.append(or_(Request.chat_id == chat_id, Request.chat_id.is_(None)))
            if candidate_request_ids:
                conditions.append(Summary.request_id.in_(candidate_request_ids))

            stmt = (
                select(Summary, Request)
                .join(Request, Summary.request_id == Request.id)
                .where(*conditions)
                .order_by(Summary.created_at.asc())
            )
            if fetch_limit is not None:
                stmt = stmt.limit(fetch_limit)

            results: list[dict[str, Any]] = []
            for summary, request in await session.execute(stmt):
                payload = ensure_mapping(summary.json_payload)
                request_data = model_to_dict(request) or {}
                if topic_query and not self._summary_matches_topic(
                    payload, request_data, topic_query
                ):
                    continue

                data = model_to_dict(summary) or {}
                flattened_request = dict(request_data)
                flattened_request.pop("id", None)
                data.update(flattened_request)
                results.append(data)
                if len(results) >= limit:
                    break
            return results

    async def async_get_unread_summary_by_request_id(
        self, request_id: int
    ) -> dict[str, Any] | None:
        """Get an unread summary by request ID."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    select(Summary, Request)
                    .join(Request, Summary.request_id == Request.id)
                    .where(Summary.request_id == request_id, Summary.is_read.is_(False))
                )
            ).first()
            if row is None:
                return None
            summary, request = row
            data = model_to_dict(summary) or {}
            request_data = model_to_dict(request) or {}
            request_data.pop("id", None)
            data.update(request_data)
            return data

    async def async_bulk_mark_summaries_as_read(
        self, *, user_id: int, summary_ids: list[int]
    ) -> int:
        """Bulk-mark summaries as read scoped to *user_id*.

        Returns the count of rows actually updated. The UPDATE joins
        through Request so a summary belonging to another user is
        silently skipped — never raises on cross-user IDs.
        """
        if not summary_ids:
            return 0
        async with self._database.transaction() as session:
            # Two-step: collect owned IDs, then UPDATE.
            owned_rows = await session.execute(
                select(Summary.id)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Summary.id.in_(summary_ids),
                    Request.user_id == user_id,
                    Summary.is_deleted.is_(False),
                    Summary.is_read.is_(False),
                )
            )
            owned_ids = [row[0] for row in owned_rows]
            if not owned_ids:
                return 0
            await session.execute(
                update(Summary)
                .where(Summary.id.in_(owned_ids))
                .values(is_read=True, updated_at=_utcnow())
            )
            return len(owned_ids)

    async def async_bulk_set_summaries_favorite(
        self, *, user_id: int, summary_ids: list[int], value: bool
    ) -> int:
        """Bulk set favorite scoped to *user_id*."""
        if not summary_ids:
            return 0
        async with self._database.transaction() as session:
            owned_rows = await session.execute(
                select(Summary.id)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Summary.id.in_(summary_ids),
                    Request.user_id == user_id,
                    Summary.is_deleted.is_(False),
                )
            )
            owned_ids = [row[0] for row in owned_rows]
            if not owned_ids:
                return 0
            await session.execute(
                update(Summary)
                .where(Summary.id.in_(owned_ids))
                .values(is_favorited=value, updated_at=_utcnow())
            )
            return len(owned_ids)

    async def async_bulk_soft_delete_summaries(
        self, *, user_id: int, summary_ids: list[int]
    ) -> int:
        """Bulk soft-delete scoped to *user_id*."""
        if not summary_ids:
            return 0
        now = _utcnow()
        async with self._database.transaction() as session:
            owned_rows = await session.execute(
                select(Summary.id)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Summary.id.in_(summary_ids),
                    Request.user_id == user_id,
                    Summary.is_deleted.is_(False),
                )
            )
            owned_ids = [row[0] for row in owned_rows]
            if not owned_ids:
                return 0
            await session.execute(
                update(Summary)
                .where(Summary.id.in_(owned_ids))
                .values(is_deleted=True, deleted_at=now, updated_at=now)
            )
            return len(owned_ids)

    async def async_mark_summary_as_read(self, summary_id: int) -> None:
        """Mark a summary as read."""
        await self._set_summary_values(summary_id, is_read=True)

    async def async_mark_summary_as_unread(self, summary_id: int) -> None:
        """Mark a summary as unread."""
        await self._set_summary_values(summary_id, is_read=False)

    async def async_mark_summary_as_read_by_request(self, request_id: int) -> None:
        """Mark a summary as read by its request ID."""
        async with self._database.transaction() as session:
            await session.execute(
                update(Summary)
                .where(Summary.request_id == request_id)
                .values(is_read=True, updated_at=_utcnow())
            )

    async def async_get_read_status(self, request_id: int) -> bool:
        """Return whether the summary for a given request is marked as read."""
        async with self._database.session() as session:
            value = await session.scalar(
                select(Summary.is_read).where(Summary.request_id == request_id)
            )
            return bool(value)

    async def async_update_reading_progress(
        self,
        summary_id: int,
        progress: float,
        last_read_offset: int,
    ) -> None:
        """Update reading progress and offset for a summary."""
        await self._set_summary_values(
            summary_id,
            reading_progress=progress,
            last_read_offset=last_read_offset,
        )

    async def async_soft_delete_summary(self, summary_id: int) -> None:
        """Soft delete a summary."""
        await self._set_summary_values(summary_id, is_deleted=True, deleted_at=_utcnow())

    async def async_toggle_favorite(self, summary_id: int) -> bool:
        """Toggle favorite status of a summary."""
        async with self._database.transaction() as session:
            summary = await session.get(Summary, summary_id, with_for_update=True)
            if summary is None:
                raise LookupError(f"summary {summary_id} not found")
            summary.is_favorited = not summary.is_favorited
            summary.updated_at = _utcnow()
            await session.flush()
            return summary.is_favorited

    async def async_set_favorite(self, summary_id: int, value: bool) -> None:
        """Persist an explicit favorite status for a summary."""
        await self._set_summary_values(summary_id, is_favorited=value)

    async def async_upsert_feedback(
        self,
        user_id: int,
        summary_id: int,
        rating: int | None,
        issues: list[str] | None,
        comment: str | None,
    ) -> dict[str, Any]:
        """Create or update feedback for a summary."""
        insert_values = {
            "user_id": user_id,
            "summary_id": summary_id,
            "rating": rating,
            "issues": json.dumps(issues) if issues is not None else None,
            "comment": comment,
        }
        update_values: dict[str, Any] = {"updated_at": _utcnow()}
        if rating is not None:
            update_values["rating"] = rating
        if issues is not None:
            update_values["issues"] = json.dumps(issues)
        if comment is not None:
            update_values["comment"] = comment

        async with self._database.transaction() as session:
            stmt = (
                insert(SummaryFeedback)
                .values(**insert_values)
                .on_conflict_do_update(
                    index_elements=[SummaryFeedback.user_id, SummaryFeedback.summary_id],
                    set_=update_values,
                )
                .returning(SummaryFeedback)
            )
            feedback = (await session.scalars(stmt)).one()

        issues_value: list[str] | None = None
        if feedback.issues:
            issues_value = json.loads(feedback.issues)
        return {
            "id": str(feedback.id),
            "rating": feedback.rating,
            "issues": issues_value,
            "comment": feedback.comment,
            "created_at": feedback.created_at,
        }

    async def async_get_summary_id_by_bot_reply(self, user_id: int, message_id: int) -> int | None:
        """Resolve the summary id for the bot reply the owner reacted to.

        The ``user_id`` predicate is a defence-in-depth IDOR guard (never drop
        it) -- a reaction must only map to a summary owned by that user.
        """
        async with self._database.session() as session:
            stmt = (
                select(Summary.id)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Request.bot_reply_message_id == message_id,
                    Request.user_id == user_id,
                )
                .limit(1)
            )
            return (await session.scalars(stmt)).first()

    async def async_get_user_summaries_for_insights(
        self,
        user_id: int,
        request_created_after: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Get summary+request rows for analytics and insight computations."""
        if limit <= 0:
            return []
        async with self._database.session() as session:
            rows = await session.execute(
                select(
                    Summary.id,
                    Summary.request_id,
                    Summary.lang,
                    Summary.json_payload,
                    Summary.version,
                    Request.created_at.label("request_created_at"),
                )
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Request.user_id == user_id,
                    Request.created_at >= request_created_after,
                    Summary.is_deleted.is_(False),
                )
                .order_by(Request.created_at.desc())
                .limit(limit)
            )
            return [
                {
                    "id": row.id,
                    "request_id": row.request_id,
                    "lang": row.lang,
                    "json_payload": row.json_payload,
                    "version": row.version,
                    "request": {"created_at": row.request_created_at},
                }
                for row in rows
            ]

    async def async_get_user_summary_activity_dates(
        self,
        user_id: int,
        created_after: datetime,
    ) -> list[Any]:
        """Return summary timestamps used for user streak calculations."""
        async with self._database.session() as session:
            rows = await session.scalars(
                select(Summary.created_at)
                .join(Request, Summary.request_id == Request.id)
                .where(
                    Request.user_id == user_id,
                    Summary.created_at >= created_after,
                    Summary.is_deleted.is_(False),
                )
                .order_by(Summary.created_at.desc())
            )
            return list(rows)

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        """Return the maximum server_version across summaries owned by *user_id*."""
        async with self._database.session() as session:
            value = await session.scalar(
                select(func.max(Summary.server_version))
                .join(Request, Summary.request_id == Request.id)
                .where(Request.user_id == user_id)
            )
            return int(value) if value is not None else None

    # 5B — bounded-memory paged snapshot for sync.  A single un-LIMIT-ed load of
    # all summaries for an active user can easily hit tens of thousands of rows
    # and exhaust the event-loop memory budget.  We page by id in
    # _SYNC_PAGE_SIZE-row batches and accumulate; the output is identical to the
    # old single-shot load (same order, no rows dropped) but each DB round-trip
    # is bounded.
    _SYNC_PAGE_SIZE: int = 500

    async def async_get_all_for_user(self, user_id: int) -> list[dict[str, Any]]:
        """Get all summaries for a user for sync operations.

        Pages internally in _SYNC_PAGE_SIZE-row batches ordered by id so that
        the full result set is collected without holding an unbounded SQLAlchemy
        cursor open. Output is identical to the previous single-shot load:
        every non-deleted AND deleted row (sync clients need tombstones) is
        returned, ordered by id ascending.
        """
        results: list[dict[str, Any]] = []
        last_id = 0
        async with self._database.session() as session:
            while True:
                rows = list(
                    (
                        await session.execute(
                            select(Summary)
                            .join(Request, Summary.request_id == Request.id)
                            .where(Request.user_id == user_id, Summary.id > last_id)
                            .order_by(Summary.id)
                            .limit(self._SYNC_PAGE_SIZE)
                        )
                    ).scalars()
                )
                if not rows:
                    break
                for row in rows:
                    results.append(model_to_dict(row) or {})
                last_id = rows[-1].id
                if len(rows) < self._SYNC_PAGE_SIZE:
                    break
        return results

    async def async_get_summary_for_sync_apply(
        self, summary_id: int, user_id: int
    ) -> dict[str, Any] | None:
        """Get a summary by ID for sync apply, validating user ownership."""
        async with self._database.session() as session:
            summary = await session.scalar(
                select(Summary)
                .join(Request, Summary.request_id == Request.id)
                .where(Summary.id == summary_id, Request.user_id == user_id)
            )
            return model_to_dict(summary)

    async def async_apply_sync_change(
        self,
        summary_id: int,
        *,
        is_deleted: bool | None = None,
        deleted_at: datetime | None = None,
        is_read: bool | None = None,
        is_favorited: bool | None = None,
    ) -> int:
        """Apply a sync change to a summary and advance its server_version.

        server_version MUST advance on every real mutation so that:
          1. Other clients calling delta-sync since their last cursor will
             observe the row as updated.
          2. A subsequent stale upload (using the pre-mutation server_version)
             is detected as a conflict by SyncApplyService instead of silently
             overwriting the new state.

        Returns the post-mutation server_version (or the existing one if the
        caller passed no fields to mutate — a no-op).
        """
        update_values: dict[str, Any] = {"updated_at": _utcnow()}
        if is_deleted is not None:
            update_values["is_deleted"] = is_deleted
        if deleted_at is not None:
            update_values["deleted_at"] = deleted_at
        if is_read is not None:
            update_values["is_read"] = is_read
        if is_favorited is not None:
            update_values["is_favorited"] = is_favorited

        # Only bump server_version when there's a real mutation. updated_at
        # alone is a heartbeat, not a sync-visible change.
        has_mutation = len(update_values) > 1
        if has_mutation:
            update_values["server_version"] = _next_server_version()

        async with self._database.transaction() as session:
            if has_mutation:
                await session.execute(
                    update(Summary).where(Summary.id == summary_id).values(**update_values)
                )
            value = await session.scalar(
                select(Summary.server_version).where(Summary.id == summary_id)
            )
            return int(value or 0)

    def to_domain_model(self, db_summary: dict[str, Any]) -> DomainSummary:
        """Convert a database record to the summary domain model."""
        return DomainSummary(
            id=db_summary.get("id"),
            request_id=db_summary.get("request_id") or db_summary.get("request"),
            content=db_summary.get("json_payload"),
            language=db_summary.get("lang"),
            version=db_summary.get("version", 1),
            is_read=db_summary.get("is_read", False),
            insights=db_summary.get("insights_json"),
            created_at=coerce_datetime(db_summary.get("created_at")),
        )

    def from_domain_model(self, summary: DomainSummary) -> dict[str, Any]:
        """Convert the summary domain model to database field values."""
        result: dict[str, Any] = {
            "request_id": summary.request_id,
            "json_payload": summary.content,
            "lang": summary.language,
            "version": summary.version,
            "is_read": summary.is_read,
        }
        if summary.id is not None:
            result["id"] = summary.id
        if summary.insights is not None:
            result["insights_json"] = summary.insights
        return result

    async def _upsert_summary_record(
        self,
        *,
        request_id: int,
        lang: str,
        json_payload: dict[str, Any],
        insights_json: dict[str, Any] | None = None,
        is_read: bool = False,
        session: Any | None = None,
    ) -> int:
        payload = prepare_json_payload(json_payload, default={})
        insights = prepare_json_payload(insights_json)
        # Extract denormalized metadata so list-view and smart-collection
        # queries can project scalar columns instead of loading json_payload.
        meta = _extract_summary_metadata(json_payload)
        insert_stmt = insert(Summary).values(
            request_id=request_id,
            lang=lang,
            json_payload=payload,
            insights_json=insights,
            is_read=is_read,
            version=1,
            **meta,
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=[Summary.request_id],
            set_={
                "lang": lang,
                "json_payload": payload,
                "insights_json": insights,
                "version": Summary.version + 1,
                "is_read": is_read,
                "updated_at": _utcnow(),
                **meta,
            },
        ).returning(Summary.version)

        if session is not None:
            return int(await session.scalar(stmt) or 1)
        async with self._database.transaction() as new_session:
            return int(await new_session.scalar(stmt) or 1)

    async def _set_summary_values(self, summary_id: int, **values: Any) -> None:
        values["updated_at"] = _utcnow()
        async with self._database.transaction() as session:
            await session.execute(update(Summary).where(Summary.id == summary_id).values(**values))

    async def _find_topic_search_request_ids(
        self, topic: str, *, candidate_limit: int
    ) -> list[int] | None:
        fts_query = self._build_tsquery(topic)
        if not fts_query:
            return None
        sql = text(
            "SELECT request_id FROM topic_search_index "
            "WHERE body_tsv @@ to_tsquery('simple', :query) "
            "ORDER BY ts_rank_cd(body_tsv, to_tsquery('simple', :query)) DESC "
            "LIMIT :limit"
        )
        try:
            async with self._database.session() as session:
                rows = await session.execute(sql, {"query": fts_query, "limit": candidate_limit})
                return [int(row[0]) for row in rows if row[0] is not None]
        except Exception:
            logger.warning("topic_search_query_failed", extra={"query": fts_query}, exc_info=True)
            return None

    def _build_tsquery(self, topic: str) -> str | None:
        terms = tokenize(topic)
        if not terms:
            sanitized = self._sanitize_fts_term(topic.casefold())
            return f"{sanitized}:*" if sanitized else None

        sanitized_terms = [self._sanitize_fts_term(term) for term in terms]
        sanitized_terms = [term for term in sanitized_terms if term]
        if not sanitized_terms:
            return None
        return " & ".join(f"{term}:*" for term in sanitized_terms)

    @staticmethod
    def _sanitize_fts_term(term: str) -> str:
        sanitized = re.sub(r"[^\w-]+", " ", term)
        return re.sub(r"\s+", " ", sanitized).strip().replace(" ", " & ")

    @staticmethod
    def _summary_matches_topic(
        summary_payload: dict[str, Any], request_data: dict[str, Any], topic: str
    ) -> bool:
        """Check if summary/request matches topic after FTS candidate selection."""
        terms = tokenize(topic)
        if not terms:
            return False

        def _yield_fragments(value: Any) -> Any:
            if isinstance(value, str):
                yield value.casefold()
            elif isinstance(value, list):
                for item in value:
                    yield from _yield_fragments(item)
            elif isinstance(value, dict):
                for key, nested in value.items():
                    yield from _yield_fragments(key)
                    yield from _yield_fragments(nested)

        fragments: list[str] = []
        fragments.extend(_yield_fragments(summary_payload))
        fragments.extend(_yield_fragments(request_data))
        combined = " ".join(fragments)
        return all(term in combined for term in terms)


def _extract_summary_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the four denormalized metadata fields from a json_payload dict.

    Returns a dict suitable for direct use as SQLAlchemy column values.
    Never raises; unknown/malformed values are coerced to None.
    """
    title = payload.get("title")
    source_type = payload.get("source_type")
    raw_rt = payload.get("estimated_reading_time_min")
    try:
        reading_time: int | None = int(raw_rt) if raw_rt is not None else None
    except (TypeError, ValueError):
        reading_time = None
    topic_tags = payload.get("topic_tags")
    return {
        "title": str(title) if title is not None else None,
        "source_type": str(source_type) if source_type is not None else None,
        "reading_time": reading_time,
        "topic_tags": topic_tags if isinstance(topic_tags, list) else None,
    }


def _status_value(status: RequestStatus | str) -> str:
    return status.value if isinstance(status, RequestStatus) else str(status)


def _aggregation_item_to_dict(
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
