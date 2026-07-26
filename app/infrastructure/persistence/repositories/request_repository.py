"""SQLAlchemy implementation of the request repository."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.json_utils import prepare_json_payload
from app.db.models import CrawlResult, Request, Summary, TelegramMessage, model_to_dict
from app.db.types import _utcnow
from app.domain.models.request import (
    Request as DomainRequest,
    RequestStatus,
    RequestType,
)

if TYPE_CHECKING:
    from datetime import datetime

    from app.db.session import Database


class RequestRepositoryAdapter:
    """Adapter for request persistence using SQLAlchemy."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_get_request_by_id(self, request_id: int) -> dict[str, Any] | None:
        async with self._database.session() as session:
            request = await session.scalar(select(Request).where(Request.id == request_id))
            return model_to_dict(request)

    async def async_get_request_context(self, request_id: int) -> dict[str, Any] | None:
        async with self._database.session() as session:
            request = await session.scalar(select(Request).where(Request.id == request_id))
            if request is None:
                return None
            crawl_result = await session.scalar(
                select(CrawlResult).where(CrawlResult.request_id == request_id)
            )
            summary = await session.scalar(select(Summary).where(Summary.request_id == request_id))
            return {
                "request": model_to_dict(request),
                "crawl_result": model_to_dict(crawl_result),
                "summary": model_to_dict(summary),
            }

    async def async_get_request_by_dedupe_hash(self, dedupe_hash: str) -> dict[str, Any] | None:
        async with self._database.session() as session:
            request = await session.scalar(
                select(Request).where(Request.dedupe_hash == dedupe_hash)
            )
            return model_to_dict(request)

    async def async_get_request_by_paper_canonical_id(
        self, paper_canonical_id: str
    ) -> dict[str, Any] | None:
        """Look up an academic-paper request by its canonical id.

        ``paper_canonical_id`` is host-namespaced (``arxiv:2301.00001``,
        ``ssrn:6531478``). Used by the academic-paper extractor to
        collapse different URL shapes pointing at the same paper.
        """
        async with self._database.session() as session:
            request = await session.scalar(
                select(Request).where(Request.paper_canonical_id == paper_canonical_id)
            )
            return model_to_dict(request)

    async def async_find_recent_request_by_dedupe(
        self, dedupe_hash: str, *, max_age_sec: int = 300
    ) -> dict[str, Any] | None:
        """Return newest processing, pending, or recently-failed request for this dedupe_hash."""
        from datetime import timedelta

        cutoff = _utcnow() - timedelta(seconds=max_age_sec)
        async with self._database.session() as session:
            request = await session.scalar(
                select(Request)
                .where(
                    Request.dedupe_hash == dedupe_hash,
                    Request.status.in_(["processing", "pending", "error"]),
                    Request.updated_at >= cutoff,
                )
                .order_by(Request.updated_at.desc())
                .limit(1)
            )
            return model_to_dict(request)

    async def async_get_latest_request_by_correlation_id(
        self, correlation_id: str
    ) -> dict[str, Any] | None:
        async with self._database.session() as session:
            request = await session.scalar(
                select(Request)
                .where(Request.correlation_id == correlation_id)
                .order_by(Request.created_at.desc())
                .limit(1)
            )
            return model_to_dict(request)

    async def async_get_requests_by_ids(
        self, request_ids: list[int], user_id: int | None = None
    ) -> dict[int, dict[str, Any]]:
        if not request_ids:
            return {}
        async with self._database.session() as session:
            stmt = select(Request).where(Request.id.in_(request_ids))
            if user_id is not None:
                stmt = stmt.where(Request.user_id == user_id)
            rows = (await session.execute(stmt)).scalars()
            return {row.id: model_to_dict(row) or {} for row in rows}

    async def async_get_request_by_forward(
        self, chat_id: int, fwd_message_id: int
    ) -> dict[str, Any] | None:
        async with self._database.session() as session:
            request = await session.scalar(
                select(Request).where(
                    Request.fwd_from_chat_id == chat_id,
                    Request.fwd_from_msg_id == fwd_message_id,
                )
            )
            return model_to_dict(request)

    async def async_get_request_error_context(self, request_id: int) -> dict[str, Any] | None:
        async with self._database.session() as session:
            value = await session.scalar(
                select(Request.error_context_json).where(Request.id == request_id)
            )
            return value if isinstance(value, dict) else None

    async def async_count_pending_requests_before(self, created_at: datetime) -> int:
        async with self._database.session() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(Request)
                    .where(Request.status == "pending", Request.created_at < created_at)
                )
                or 0
            )

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        async with self._database.session() as session:
            value = await session.scalar(
                select(func.max(Request.server_version)).where(Request.user_id == user_id)
            )
            return int(value) if value is not None else None

    async def async_get_all_for_user(self, user_id: int, *, since: int = 0) -> list[dict[str, Any]]:
        stmt = select(Request).where(Request.user_id == user_id)
        if since > 0:
            # Incremental sync: only rows changed past the client's cursor, pushed to
            # the DB so a poll never re-reads the user's entire lifetime history and
            # discards it in memory (audit #2). server_version is a global monotonic
            # counter, so this is equivalent to the caller's server_version > since
            # pagination filter.
            stmt = stmt.where(Request.server_version > since)
        stmt = stmt.order_by(Request.id)
        async with self._database.session() as session:
            rows = (await session.execute(stmt)).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def async_get_request_id_by_url_with_summary(self, user_id: int, url: str) -> int | None:
        async with self._database.session() as session:
            return await session.scalar(
                select(Request.id)
                .join(Summary, Summary.request_id == Request.id)
                .where(
                    Request.user_id == user_id,
                    or_(Request.input_url == url, Request.normalized_url == url),
                )
                .order_by(Request.created_at.desc())
                .limit(1)
            )

    async def async_get_request_by_telegram_message(
        self,
        *,
        user_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        async with self._database.session() as session:
            request = await session.scalar(
                select(Request)
                .where(
                    Request.user_id == user_id,
                    or_(
                        Request.bot_reply_message_id == message_id,
                        Request.input_message_id == message_id,
                    ),
                )
                .order_by(Request.created_at.desc())
                .limit(1)
            )
            return model_to_dict(request)

    async def async_insert_telegram_message(
        self,
        *,
        request_id: int,
        message_id: int | None,
        chat_id: int | None,
        date_ts: int | None,
        text_full: str | None,
        entities_json: Any,
        media_type: str | None,
        media_file_ids_json: Any,
        forward_from_chat_id: int | None,
        forward_from_chat_type: str | None,
        forward_from_chat_title: str | None,
        forward_from_message_id: int | None,
        forward_date_ts: int | None,
        telegram_raw_json: Any,
    ) -> int:
        payload = {
            "request_id": request_id,
            "message_id": message_id,
            "chat_id": chat_id,
            "date_ts": date_ts,
            "text_full": text_full,
            "entities_json": prepare_json_payload(entities_json),
            "media_type": media_type,
            "media_file_ids_json": prepare_json_payload(media_file_ids_json),
            "forward_from_chat_id": forward_from_chat_id,
            "forward_from_chat_type": forward_from_chat_type,
            "forward_from_chat_title": forward_from_chat_title,
            "forward_from_message_id": forward_from_message_id,
            "forward_date_ts": forward_date_ts,
            "telegram_raw_json": prepare_json_payload(telegram_raw_json),
        }
        async with self._database.transaction() as session:
            stmt = (
                insert(TelegramMessage)
                .values(**payload)
                .on_conflict_do_nothing(index_elements=[TelegramMessage.request_id])
                .returning(TelegramMessage.id)
            )
            inserted_id = await session.scalar(stmt)
            if inserted_id is not None:
                return int(inserted_id)
            existing_id = await session.scalar(
                select(TelegramMessage.id).where(TelegramMessage.request_id == request_id)
            )
            if existing_id is None:
                msg = f"telegram message conflict for request_id={request_id} but no row exists"
                raise RuntimeError(msg)
            return int(existing_id)

    async def async_update_bot_reply_message_id(
        self, request_id: int, bot_reply_message_id: int
    ) -> None:
        await self._update_request(request_id, bot_reply_message_id=bot_reply_message_id)

    async def async_create_request(
        self,
        *,
        type_: str = "url",
        status: RequestStatus = RequestStatus.PENDING,
        correlation_id: str | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
        input_url: str | None = None,
        normalized_url: str | None = None,
        dedupe_hash: str | None = None,
        paper_canonical_id: str | None = None,
        input_message_id: int | None = None,
        fwd_from_chat_id: int | None = None,
        fwd_from_msg_id: int | None = None,
        lang_detected: str | None = None,
        content_text: str | None = None,
        route_version: int = 1,
        initial_attempt_trigger: str | None = None,
    ) -> int:
        request_id, _created = await self.async_create_request_once(
            type_=type_,
            status=status,
            correlation_id=correlation_id,
            chat_id=chat_id,
            user_id=user_id,
            input_url=input_url,
            normalized_url=normalized_url,
            dedupe_hash=dedupe_hash,
            paper_canonical_id=paper_canonical_id,
            input_message_id=input_message_id,
            fwd_from_chat_id=fwd_from_chat_id,
            fwd_from_msg_id=fwd_from_msg_id,
            lang_detected=lang_detected,
            content_text=content_text,
            route_version=route_version,
            initial_attempt_trigger=initial_attempt_trigger,
        )
        return request_id

    async def async_create_request_once(
        self,
        *,
        type_: str = "url",
        status: RequestStatus = RequestStatus.PENDING,
        correlation_id: str | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
        input_url: str | None = None,
        normalized_url: str | None = None,
        dedupe_hash: str | None = None,
        paper_canonical_id: str | None = None,
        input_message_id: int | None = None,
        fwd_from_chat_id: int | None = None,
        fwd_from_msg_id: int | None = None,
        lang_detected: str | None = None,
        content_text: str | None = None,
        route_version: int = 1,
        initial_attempt_trigger: str | None = None,
    ) -> tuple[int, bool]:
        payload = {
            "user_id": user_id,
            "chat_id": chat_id,
            "input_url": input_url,
            "normalized_url": normalized_url,
            "lang_detected": lang_detected,
            "input_message_id": input_message_id,
            "fwd_from_chat_id": fwd_from_chat_id,
            "fwd_from_msg_id": fwd_from_msg_id,
            "dedupe_hash": dedupe_hash,
            "paper_canonical_id": paper_canonical_id,
            "correlation_id": correlation_id,
            "type": type_,
            "status": _status_value(status),
            "content_text": content_text,
            "route_version": route_version,
            "initial_attempt_trigger": initial_attempt_trigger,
        }
        # Dedupe key precedence: paper_canonical_id (academic papers) takes
        # priority because it survives URL-shape variations (/abs vs /pdf,
        # v1 vs v2); dedupe_hash is the URL-level fallback for everything
        # else. We can only declare one ON CONFLICT target per statement,
        # so the academic path uses paper_canonical_id and the rest use
        # dedupe_hash.
        conflict_target: str | None
        conflict_column: Any | None
        if paper_canonical_id:
            conflict_target = "paper_canonical_id"
            conflict_column = Request.paper_canonical_id
        elif dedupe_hash:
            conflict_target = "dedupe_hash"
            conflict_column = Request.dedupe_hash
        else:
            conflict_target = None
            conflict_column = None
        async with self._database.transaction() as session:
            stmt = insert(Request).values(**payload)
            if conflict_target and conflict_column is not None:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=[Request.user_id, conflict_column],
                    index_where=conflict_column.is_not(None),
                )
            returning_stmt = stmt.returning(Request.id)
            inserted_id = await session.scalar(returning_stmt)
            if inserted_id is not None:
                return int(inserted_id), True
            if conflict_target is None:
                msg = "request insert did not return an id"
                raise RuntimeError(msg)
            existing_id = await session.scalar(
                select(Request.id).where(
                    Request.user_id == user_id,
                    getattr(Request, conflict_target) == payload[conflict_target],
                )
            )
            if existing_id is None:
                msg = f"request conflict on {conflict_target} did not return an existing id"
                raise RuntimeError(msg)
            return int(existing_id), False

    async def async_create_minimal_request(
        self,
        *,
        type_: str = "url",
        status: RequestStatus = RequestStatus.PENDING,
        correlation_id: str | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
        input_url: str | None = None,
        normalized_url: str | None = None,
        dedupe_hash: str | None = None,
    ) -> tuple[int, bool]:
        payload = {
            "type": type_,
            "status": _status_value(status),
            "correlation_id": correlation_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "input_url": input_url,
            "normalized_url": normalized_url,
            "dedupe_hash": dedupe_hash,
        }
        async with self._database.transaction() as session:
            stmt = insert(Request).values(**payload)
            if dedupe_hash:
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=[Request.user_id, Request.dedupe_hash],
                    index_where=Request.dedupe_hash.is_not(None),
                )
            returning_stmt = stmt.returning(Request.id)
            inserted_id = await session.scalar(returning_stmt)
            if inserted_id is not None:
                return int(inserted_id), True
            existing_id = await session.scalar(
                select(Request.id).where(
                    Request.user_id == user_id,
                    Request.dedupe_hash == dedupe_hash,
                )
            )
            if existing_id is None:
                msg = "request conflict did not return an existing id"
                raise RuntimeError(msg)
            return int(existing_id), False

    async def async_update_request_status(self, request_id: int, status: str) -> None:
        await self._update_request(request_id, status=status)

    async def async_update_request_status_with_correlation(
        self, request_id: int, status: str, correlation_id: str | None
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if correlation_id:
            values["correlation_id"] = correlation_id
        await self._update_request(request_id, **values)

    async def async_update_request_correlation_id(
        self, request_id: int, correlation_id: str
    ) -> None:
        await self._update_request(request_id, correlation_id=correlation_id)

    async def async_update_request_content_text(self, request_id: int, content_text: str) -> None:
        await self._update_request(request_id, content_text=content_text)

    async def async_update_request_lang_detected(self, request_id: int, lang: str) -> None:
        await self._update_request(request_id, lang_detected=lang)

    async def async_update_request_error(
        self,
        request_id: int,
        status: str,
        error_type: str | None = None,
        error_message: str | None = None,
        processing_time_ms: int | None = None,
        error_context_json: Any | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status, "error_timestamp": _utcnow()}
        if error_type is not None:
            values["error_type"] = error_type
        if error_message is not None:
            values["error_message"] = error_message
        if processing_time_ms is not None:
            values["processing_time_ms"] = processing_time_ms
        if error_context_json is not None:
            values["error_context_json"] = prepare_json_payload(error_context_json, default={})
        await self._update_request(request_id, **values)

    async def _update_request(self, request_id: int, **values: Any) -> None:
        async with self._database.transaction() as session:
            await session.execute(update(Request).where(Request.id == request_id).values(**values))

    def to_domain_model(self, db_request: dict[str, Any]) -> DomainRequest:
        request_type_str = db_request.get("type", "unknown")
        try:
            request_type = RequestType(request_type_str)
        except ValueError:
            request_type = RequestType.UNKNOWN

        status_str = db_request.get("status", "pending")
        try:
            status = RequestStatus(status_str)
        except ValueError:
            status = RequestStatus.PENDING

        return DomainRequest(
            id=db_request.get("id"),
            user_id=db_request["user_id"],
            chat_id=db_request["chat_id"],
            request_type=request_type,
            status=status,
            input_url=db_request.get("input_url"),
            normalized_url=db_request.get("normalized_url"),
            dedupe_hash=db_request.get("dedupe_hash"),
            correlation_id=db_request.get("correlation_id"),
            input_message_id=db_request.get("input_message_id"),
            fwd_from_chat_id=db_request.get("fwd_from_chat_id"),
            fwd_from_msg_id=db_request.get("fwd_from_msg_id"),
            lang_detected=db_request.get("lang_detected"),
            content_text=db_request.get("content_text"),
            route_version=db_request.get("route_version", 1),
            created_at=db_request.get("created_at", _utcnow()),
        )

    def from_domain_model(self, request: DomainRequest) -> dict[str, Any]:
        result: dict[str, Any] = {
            "user_id": request.user_id,
            "chat_id": request.chat_id,
            "type": request.request_type.value,
            "status": request.status.value,
            "route_version": request.route_version,
        }

        for attr in (
            "id",
            "input_url",
            "normalized_url",
            "dedupe_hash",
            "correlation_id",
            "input_message_id",
            "fwd_from_chat_id",
            "fwd_from_msg_id",
            "lang_detected",
            "content_text",
        ):
            value = getattr(request, attr)
            if value is not None:
                result[attr] = value
        return result


def _status_value(status: RequestStatus | str) -> str:
    return status.value if isinstance(status, RequestStatus) else status
