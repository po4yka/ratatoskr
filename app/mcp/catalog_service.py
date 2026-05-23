from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.core.call_status import CallStatus
from app.mcp.helpers import McpErrorResult, format_summary_compact, isotime, paginated_payload

logger = logging.getLogger("ratatoskr.mcp")

if TYPE_CHECKING:
    from app.mcp.context import McpServerContext


class CatalogReadService:
    def __init__(self, context: McpServerContext) -> None:
        self.context = context

    async def list_collections(
        self, limit: int = 20, offset: int = 0
    ) -> dict[str, Any] | McpErrorResult:
        from app.db.models import Collection, CollectionItem

        limit = max(1, min(50, limit))
        offset = max(0, offset)

        try:
            filters = [
                Collection.parent_id.is_(None),
                *self.context.collection_scope_filters(Collection),
            ]
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                total = await session.scalar(
                    select(func.count()).select_from(Collection).where(*filters)
                )
                collections = (
                    await session.scalars(
                        select(Collection)
                        .where(*filters)
                        .order_by(Collection.position, Collection.created_at.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()

                collection_ids = [collection.id for collection in collections]
                item_counts: dict[int, int] = {}
                child_counts: dict[int, int] = {}
                if collection_ids:
                    item_count_rows = (
                        await session.execute(
                            select(CollectionItem.collection_id, func.count())
                            .where(CollectionItem.collection_id.in_(collection_ids))
                            .group_by(CollectionItem.collection_id)
                        )
                    ).all()
                    item_counts = {
                        collection_id: int(count or 0) for collection_id, count in item_count_rows
                    }
                    child_count_rows = (
                        await session.execute(
                            select(Collection.parent_id, func.count())
                            .where(
                                Collection.parent_id.in_(collection_ids),
                                *self.context.collection_scope_filters(Collection),
                            )
                            .group_by(Collection.parent_id)
                        )
                    ).all()
                    child_counts = {
                        collection_id: int(count or 0)
                        for collection_id, count in child_count_rows
                        if collection_id is not None
                    }

                results = []
                for collection in collections:
                    results.append(
                        {
                            "collection_id": collection.id,
                            "name": collection.name,
                            "description": collection.description,
                            "item_count": item_counts.get(collection.id, 0),
                            "child_collections": child_counts.get(collection.id, 0),
                            "is_shared": collection.is_shared,
                            "created_at": isotime(collection.created_at),
                            "updated_at": isotime(collection.updated_at),
                        }
                    )

            return paginated_payload(results=results, total=total or 0, limit=limit, offset=offset)
        except Exception as exc:
            logger.exception("list_collections failed")
            return {"error": str(exc)}

    async def get_collection(
        self,
        collection_id: int,
        include_items: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        from app.db.models import (
            Collection,
            CollectionItem,
            Request,
            Summary,
        )

        limit = max(1, min(100, limit))

        try:
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                collection = await session.scalar(
                    select(Collection).where(
                        Collection.id == collection_id,
                        *self.context.collection_scope_filters(Collection),
                    )
                )
                if not collection:
                    return {"error": f"Collection {collection_id} not found"}

                children = (
                    await session.scalars(
                        select(Collection)
                        .where(
                            Collection.parent_id == collection.id,
                            *self.context.collection_scope_filters(Collection),
                        )
                        .order_by(Collection.position, Collection.created_at)
                    )
                ).all()
                result: dict[str, Any] = {
                    "collection_id": collection.id,
                    "name": collection.name,
                    "description": collection.description,
                    "is_shared": collection.is_shared,
                    "child_collections": [
                        {
                            "collection_id": child.id,
                            "name": child.name,
                            "description": child.description,
                        }
                        for child in children
                    ],
                    "created_at": isotime(collection.created_at),
                    "updated_at": isotime(collection.updated_at),
                }

                if include_items:
                    items = (
                        await session.scalars(
                            select(CollectionItem)
                            .join(Summary, CollectionItem.summary_id == Summary.id)
                            .join(Request, Summary.request_id == Request.id)
                            .options(
                                selectinload(CollectionItem.summary).selectinload(Summary.request)
                            )
                            .where(
                                CollectionItem.collection_id == collection.id,
                                Summary.is_deleted.is_(False),
                                *self.context.request_scope_filters(Request),
                            )
                            .order_by(CollectionItem.position, CollectionItem.created_at)
                            .limit(limit)
                        )
                    ).all()
                    articles = [
                        format_summary_compact(item.summary, item.summary.request) for item in items
                    ]
                    result["articles"] = articles
                    result["article_count"] = len(articles)

                return result
        except Exception as exc:
            logger.exception("get_collection failed")
            return {"error": str(exc)}

    async def list_videos(
        self,
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> dict[str, Any]:
        from app.db.models import Request, VideoDownload

        limit = max(1, min(50, limit))
        offset = max(0, offset)

        try:
            filters = [*self.context.request_scope_filters(Request)]
            if status and status in ("pending", "downloading", "completed", "error"):
                filters.append(VideoDownload.status == status)

            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                total = await session.scalar(
                    select(func.count())
                    .select_from(VideoDownload)
                    .join(Request, VideoDownload.request_id == Request.id)
                    .where(*filters)
                )
                videos = (
                    await session.scalars(
                        select(VideoDownload)
                        .join(Request, VideoDownload.request_id == Request.id)
                        .options(selectinload(VideoDownload.request))
                        .where(*filters)
                        .order_by(VideoDownload.created_at.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()

                results = [self._format_video(video) for video in videos]

            return paginated_payload(results=results, total=total or 0, limit=limit, offset=offset)
        except Exception as exc:
            logger.exception("list_videos failed")
            return {"error": str(exc)}

    async def get_video_transcript(self, video_id: str) -> dict[str, Any]:
        from app.db.models import Request, VideoDownload

        try:
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                video = await session.scalar(
                    select(VideoDownload)
                    .join(Request, VideoDownload.request_id == Request.id)
                    .options(selectinload(VideoDownload.request))
                    .where(
                        VideoDownload.video_id == video_id,
                        *self.context.request_scope_filters(Request),
                    )
                )
                if not video:
                    return {"error": f"Video {video_id} not found"}
                if not video.transcript_text:
                    return {
                        "video_id": video_id,
                        "title": video.title,
                        "error": "No transcript available for this video",
                    }

                return {
                    "video_id": video_id,
                    "title": video.title,
                    "channel": video.channel,
                    "duration_sec": video.duration_sec,
                    "transcript_source": video.transcript_source,
                    "subtitle_language": video.subtitle_language,
                    "auto_generated": video.auto_generated,
                    "transcript": video.transcript_text[:50000],
                    "transcript_length": len(video.transcript_text),
                    "truncated": len(video.transcript_text) > 50000,
                }
        except Exception as exc:
            logger.exception("get_video_transcript failed")
            return {"error": str(exc)}

    async def processing_stats(self) -> dict[str, Any]:
        from app.db.models import LLMCall, Request, VideoDownload

        try:
            request_filters = self.context.request_scope_filters(Request)
            llm_filters = [
                LLMCall.is_deleted.is_(False),
                *request_filters,
            ]
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                total_calls = await session.scalar(
                    select(func.count())
                    .select_from(LLMCall)
                    .join(Request, LLMCall.request_id == Request.id)
                    .where(*llm_filters)
                )
                success_calls = await session.scalar(
                    select(func.count())
                    .select_from(LLMCall)
                    .join(Request, LLMCall.request_id == Request.id)
                    .where(*llm_filters, LLMCall.status == CallStatus.OK.value)
                )
                error_calls = await session.scalar(
                    select(func.count())
                    .select_from(LLMCall)
                    .join(Request, LLMCall.request_id == Request.id)
                    .where(*llm_filters, LLMCall.status == CallStatus.ERROR.value)
                )

                token_stats = (
                    await session.execute(
                        select(
                            func.sum(LLMCall.tokens_prompt).label("total_prompt"),
                            func.sum(LLMCall.tokens_completion).label("total_completion"),
                            func.sum(LLMCall.cost_usd).label("total_cost"),
                            func.avg(LLMCall.latency_ms).label("avg_latency_ms"),
                        )
                        .join(Request, LLMCall.request_id == Request.id)
                        .where(*llm_filters, LLMCall.status == CallStatus.OK.value)
                    )
                ).mappings().first() or {}

                model_rows = (
                    await session.execute(
                        select(LLMCall.model, func.count().label("count"))
                        .join(Request, LLMCall.request_id == Request.id)
                        .where(
                            *llm_filters,
                            LLMCall.status == CallStatus.OK.value,
                            LLMCall.model.is_not(None),
                        )
                        .group_by(LLMCall.model)
                        .order_by(func.count().desc())
                        .limit(10)
                    )
                ).all()

                video_base_filters = self.context.request_scope_filters(Request)
                total_videos = await session.scalar(
                    select(func.count())
                    .select_from(VideoDownload)
                    .join(Request, VideoDownload.request_id == Request.id)
                    .where(*video_base_filters)
                )
                completed_videos = await session.scalar(
                    select(func.count())
                    .select_from(VideoDownload)
                    .join(Request, VideoDownload.request_id == Request.id)
                    .where(*video_base_filters, VideoDownload.status == "completed")
                )
                videos_with_transcript = await session.scalar(
                    select(func.count())
                    .select_from(VideoDownload)
                    .join(Request, VideoDownload.request_id == Request.id)
                    .where(
                        *video_base_filters,
                        VideoDownload.status == "completed",
                        VideoDownload.transcript_text.is_not(None),
                    )
                )

            total_calls = total_calls or 0
            success_calls = success_calls or 0
            error_calls = error_calls or 0
            return {
                "llm_calls": {
                    "total": total_calls,
                    "success": success_calls,
                    "errors": error_calls,
                    "success_rate": round(success_calls / total_calls * 100, 1)
                    if total_calls
                    else 0,
                },
                "token_usage": {
                    "total_prompt_tokens": token_stats.get("total_prompt"),
                    "total_completion_tokens": token_stats.get("total_completion"),
                    "total_cost_usd": (
                        round(float(token_stats["total_cost"]), 4)
                        if token_stats.get("total_cost")
                        else None
                    ),
                    "avg_latency_ms": (
                        round(float(token_stats["avg_latency_ms"]))
                        if token_stats.get("avg_latency_ms")
                        else None
                    ),
                },
                "top_models": [
                    {"model": model or "unknown", "calls": count} for model, count in model_rows
                ],
                "videos": {
                    "total": total_videos or 0,
                    "completed": completed_videos or 0,
                    "with_transcript": videos_with_transcript or 0,
                },
            }
        except Exception as exc:
            logger.exception("processing_stats_resource failed")
            return {"error": str(exc)}

    @staticmethod
    def _format_video(video: Any) -> dict[str, Any]:
        request = video.request
        return {
            "video_id": video.video_id,
            "request_id": request.id,
            "url": getattr(request, "input_url", ""),
            "title": video.title,
            "channel": video.channel,
            "duration_sec": video.duration_sec,
            "duration_display": (
                f"{video.duration_sec // 60}:{video.duration_sec % 60:02d}"
                if video.duration_sec
                else None
            ),
            "resolution": video.resolution,
            "view_count": video.view_count,
            "like_count": video.like_count,
            "has_transcript": bool(video.transcript_text),
            "transcript_source": video.transcript_source,
            "status": video.status,
            "upload_date": video.upload_date,
            "created_at": isotime(video.created_at),
        }
