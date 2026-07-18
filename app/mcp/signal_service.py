"""MCP service for signal sources and triage queue."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.mcp.context import McpServerContext

logger = get_logger(__name__)


class SignalMcpService:
    def __init__(self, context: McpServerContext) -> None:
        self.context = context

    async def list_sources(self, limit: int = 50) -> dict[str, Any]:
        from app.db.models import Source

        runtime = self.context.ensure_runtime()
        async with runtime.database.session() as session:
            rows = (
                await session.scalars(
                    select(Source).order_by(Source.created_at.desc()).limit(max(1, min(limit, 100)))
                )
            ).all()
        return {"sources": [self._source(row) for row in rows]}

    async def list_signals(self, limit: int = 20, status: str | None = None) -> dict[str, Any]:
        from app.db.models import FeedItem, UserSignal

        query = select(UserSignal).options(
            selectinload(UserSignal.feed_item).selectinload(FeedItem.source),
            selectinload(UserSignal.topic),
        )
        if self.context.user_id is not None:
            query = query.where(UserSignal.user_id == self.context.user_id)
        if status:
            query = query.where(UserSignal.status == status)
        query = query.order_by(
            UserSignal.final_score.desc().nulls_last(),
            UserSignal.created_at.desc(),
        ).limit(max(1, min(limit, 100)))

        runtime = self.context.ensure_runtime()
        async with runtime.database.session() as session:
            rows = (await session.scalars(query)).all()
        return {"signals": [self._signal(row) for row in rows]}

    async def update_signal_feedback(self, signal_id: int, action: str) -> dict[str, Any]:
        from app.infrastructure.persistence.repositories.signal_source_repository import (
            SignalSourceRepositoryAdapter,
        )

        runtime = await self.context.ensure_api_runtime()
        repo = SignalSourceRepositoryAdapter(runtime.db)
        user_id = self.context.user_id
        if user_id is None:
            return {"error": "Signal feedback requires a scoped MCP user"}
        if action == "hide_source":
            updated = await repo.async_hide_signal_source(user_id=user_id, signal_id=signal_id)
        elif action == "boost_topic":
            updated = await repo.async_boost_signal_topic(user_id=user_id, signal_id=signal_id)
        else:
            status = {
                "like": "liked",
                "dislike": "dismissed",
                "skip": "skipped",
                "queue": "queued",
            }.get(action)
            if status is None:
                return {"error": f"Unsupported feedback action: {action}"}
            updated = await repo.async_update_user_signal_status(
                user_id=user_id,
                signal_id=signal_id,
                status=status,
            )
        return {"updated": bool(updated)}

    async def promote_to_library(self, source_type: str, source_id: int) -> dict[str, Any]:
        """Create and durably enqueue a summary request for one signal or X bookmark."""
        correlation_id = f"mcp-promotion-{uuid4().hex}"
        user_id = self.context.user_id
        if user_id is None:
            return self._promotion_error(
                "Library promotion requires a scoped MCP user", correlation_id
            )
        if source_type not in {"signal", "x_bookmark"}:
            return self._promotion_error(
                f"Unsupported promotion source type: {source_type}", correlation_id
            )

        try:
            runtime = await self.context.ensure_api_runtime()
            input_url = await self._promotion_url(
                runtime=runtime,
                source_type=source_type,
                source_id=source_id,
                user_id=user_id,
            )
        except Exception:
            logger.exception(
                "library_promotion_source_lookup_failed",
                extra={"correlation_id": correlation_id, "source_type": source_type},
            )
            return self._promotion_error("Unable to load promotion source", correlation_id)
        if input_url is None:
            return self._promotion_error(
                "Promotion source was not found or has no promotable URL",
                correlation_id,
                source_type=source_type,
                source_id=source_id,
            )

        try:
            duplicate = await runtime.request_service.check_duplicate_url(user_id, input_url)
        except Exception:
            logger.exception(
                "library_promotion_duplicate_check_failed",
                extra={"correlation_id": correlation_id, "source_type": source_type},
            )
            return self._promotion_error("Unable to check the library", correlation_id)
        if duplicate is not None:
            return {
                "promoted": False,
                "source_type": source_type,
                "source_id": source_id,
                "request_id": duplicate.existing_request_id,
                "status": "already_in_library",
                "duplicate": True,
            }

        try:
            if source_type == "x_bookmark":
                request_id = await self._activate_x_bookmark(
                    runtime=runtime,
                    source_id=source_id,
                    user_id=user_id,
                    correlation_id=correlation_id,
                )
                if request_id is None:
                    return self._promotion_error(
                        "X bookmark is no longer available for promotion",
                        correlation_id,
                        source_type=source_type,
                        source_id=source_id,
                    )
            else:
                created = await runtime.request_service.create_url_request(
                    user_id=user_id,
                    input_url=input_url,
                    correlation_id=correlation_id,
                )
                request_id = created.id
        except Exception:
            logger.exception(
                "library_promotion_request_create_failed",
                extra={"correlation_id": correlation_id, "source_type": source_type},
            )
            return self._promotion_error("Unable to create summary request", correlation_id)

        if source_type == "signal":
            archived = await self._archive_promoted_signal(
                runtime,
                user_id=user_id,
                signal_id=source_id,
                correlation_id=correlation_id,
            )
            if not archived:
                error_message = f"Unable to archive queued signal. Error ID: {correlation_id}"
                await self._mark_enqueue_failed(
                    runtime,
                    user_id=user_id,
                    request_id=request_id,
                    error_message=error_message,
                    correlation_id=correlation_id,
                )
                return self._promotion_error(
                    "Unable to archive queued signal",
                    correlation_id,
                    source_type=source_type,
                    source_id=source_id,
                )

        try:
            await runtime.durable_request_queue.enqueue(
                request_id=request_id,
                correlation_id=correlation_id,
            )
        except Exception:
            error_message = f"Unable to enqueue summary request. Error ID: {correlation_id}"
            logger.exception(
                "library_promotion_enqueue_failed",
                extra={"correlation_id": correlation_id, "request_id": request_id},
            )
            await self._mark_enqueue_failed(
                runtime,
                user_id=user_id,
                request_id=request_id,
                error_message=error_message,
                correlation_id=correlation_id,
            )
            if source_type == "signal":
                await self._restore_queued_signal(
                    runtime,
                    user_id=user_id,
                    signal_id=source_id,
                    correlation_id=correlation_id,
                )
            return self._promotion_error(
                "Unable to enqueue summary request",
                correlation_id,
                source_type=source_type,
                source_id=source_id,
            )

        return {
            "promoted": True,
            "source_type": source_type,
            "source_id": source_id,
            "request_id": request_id,
            "status": "queued",
            "duplicate": False,
        }

    @staticmethod
    async def _promotion_url(
        *,
        runtime: Any,
        source_type: str,
        source_id: int,
        user_id: int,
    ) -> str | None:
        async with runtime.db.session() as session:
            if source_type == "signal":
                from app.db.models import FeedItem, UserSignal

                signal = await session.scalar(
                    select(UserSignal)
                    .options(selectinload(UserSignal.feed_item).selectinload(FeedItem.source))
                    .where(
                        UserSignal.id == source_id,
                        UserSignal.user_id == user_id,
                        UserSignal.status == "queued",
                    )
                )
                return signal.feed_item.canonical_url if signal is not None else None

            from app.db.models import Request

            bookmark = await session.scalar(
                select(Request).where(
                    Request.id == source_id,
                    Request.type == "x_bookmark",
                    Request.status == "x_imported",
                    or_(Request.user_id == user_id, Request.user_id.is_(None)),
                )
            )
            return bookmark.input_url if bookmark is not None else None

    @staticmethod
    async def _activate_x_bookmark(
        *,
        runtime: Any,
        source_id: int,
        user_id: int,
        correlation_id: str,
    ) -> int | None:
        from app.db.models import Request

        async with runtime.db.transaction() as session:
            bookmark = await session.scalar(
                select(Request)
                .where(
                    Request.id == source_id,
                    Request.type == "x_bookmark",
                    Request.status == "x_imported",
                    or_(Request.user_id == user_id, Request.user_id.is_(None)),
                )
                .with_for_update()
            )
            if bookmark is None:
                return None
            bookmark.user_id = user_id
            bookmark.status = "pending"
            bookmark.correlation_id = correlation_id
            bookmark.error_type = None
            bookmark.error_message = None
            bookmark.error_timestamp = None
            await session.flush()
            return int(bookmark.id)

    @staticmethod
    async def _archive_promoted_signal(
        runtime: Any,
        *,
        user_id: int,
        signal_id: int,
        correlation_id: str,
    ) -> bool:
        from app.infrastructure.persistence.repositories.signal_source_repository import (
            SignalSourceRepositoryAdapter,
        )

        repo = SignalSourceRepositoryAdapter(runtime.db)
        try:
            return await repo.async_update_user_signal_status(
                user_id=user_id,
                signal_id=signal_id,
                status="archived",
            )
        except Exception:
            logger.exception(
                "library_promotion_signal_archive_failed",
                extra={
                    "signal_id": signal_id,
                    "user_id": user_id,
                    "correlation_id": correlation_id,
                },
            )
            return False

    @staticmethod
    async def _restore_queued_signal(
        runtime: Any,
        *,
        user_id: int,
        signal_id: int,
        correlation_id: str,
    ) -> None:
        from app.infrastructure.persistence.repositories.signal_source_repository import (
            SignalSourceRepositoryAdapter,
        )

        repo = SignalSourceRepositoryAdapter(runtime.db)
        try:
            await repo.async_update_user_signal_status(
                user_id=user_id,
                signal_id=signal_id,
                status="queued",
            )
        except Exception:
            logger.exception(
                "library_promotion_signal_restore_failed",
                extra={
                    "signal_id": signal_id,
                    "user_id": user_id,
                    "correlation_id": correlation_id,
                },
            )

    @staticmethod
    async def _mark_enqueue_failed(
        runtime: Any,
        *,
        user_id: int,
        request_id: int,
        error_message: str,
        correlation_id: str,
    ) -> None:
        try:
            await runtime.request_service.mark_enqueue_failed(
                user_id=user_id,
                request_id=request_id,
                error_message=error_message,
            )
        except Exception:
            logger.exception(
                "library_promotion_enqueue_failure_persist_failed",
                extra={"correlation_id": correlation_id, "request_id": request_id},
            )

    @staticmethod
    def _promotion_error(
        message: str,
        correlation_id: str,
        **details: Any,
    ) -> dict[str, Any]:
        return {
            "error": f"{message}. Error ID: {correlation_id}",
            "correlation_id": correlation_id,
            **details,
        }

    async def set_source_active(self, source_id: int, is_active: bool) -> dict[str, Any]:
        from app.infrastructure.persistence.repositories.signal_source_repository import (
            SignalSourceRepositoryAdapter,
        )

        runtime = await self.context.ensure_api_runtime()
        repo = SignalSourceRepositoryAdapter(runtime.db)
        user_id = self.context.user_id
        if user_id is None:
            return {"error": "Source updates require a scoped MCP user"}
        updated = await repo.async_set_user_source_active(
            user_id=user_id,
            source_id=source_id,
            is_active=is_active,
        )
        return {"updated": bool(updated), "is_active": bool(is_active)}

    @staticmethod
    def _source(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "kind": row.kind,
            "external_id": row.external_id,
            "url": row.url,
            "title": row.title,
            "is_active": row.is_active,
            "fetch_error_count": row.fetch_error_count,
            "last_error": row.last_error,
        }

    @staticmethod
    def _signal(row: Any) -> dict[str, Any]:
        return {
            "id": row.id,
            "status": row.status,
            "final_score": row.final_score,
            "filter_stage": row.filter_stage,
            "title": row.feed_item.title,
            "url": row.feed_item.canonical_url,
            "source_kind": row.feed_item.source.kind,
            "source_title": row.feed_item.source.title,
            "topic_name": row.topic.name if row.topic_id else None,
        }
