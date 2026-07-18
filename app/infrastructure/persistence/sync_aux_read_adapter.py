"""Auxiliary SQLAlchemy reads for sync records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, select

from app.db.models import (
    CrawlResult,
    LLMCall,
    Request,
    Summary,
    SummaryHighlight,
    SummaryTag,
    Tag,
    User,
    model_to_dict,
)

if TYPE_CHECKING:
    from app.db.session import Database


class SyncAuxReadAdapter:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_sync_page(
        self,
        entity_type: str,
        user_id: int,
        *,
        since: int,
        limit: int | None,
        through_version: int | None,
    ) -> list[dict[str, Any]]:
        """Return a bounded, wire-shaped projection ordered by keyset columns."""
        if limit is not None and limit <= 0:
            return []
        stmt = self._build_sync_statement(entity_type, user_id)
        if stmt is None:
            return []

        version_column, id_column = self._keyset_columns(entity_type)
        stmt = stmt.where(version_column > since)
        if through_version is not None:
            stmt = stmt.where(version_column <= through_version)
        stmt = stmt.order_by(version_column.asc(), id_column.asc())
        if limit is not None:
            stmt = stmt.limit(limit)

        async with self._database.session() as session:
            rows = (await session.execute(stmt)).mappings()
            return [dict(row) for row in rows]

    @staticmethod
    def _keyset_columns(entity_type: str) -> tuple[Any, Any]:
        columns = {
            "user": (User.server_version, User.telegram_user_id),
            "request": (Request.server_version, Request.id),
            "summary": (Summary.server_version, Summary.id),
            "crawl_result": (CrawlResult.server_version, CrawlResult.id),
            "llm_call": (LLMCall.server_version, LLMCall.id),
            "highlight": (SummaryHighlight.server_version, SummaryHighlight.id),
            "tag": (Tag.server_version, Tag.id),
            "summary_tag": (SummaryTag.server_version, SummaryTag.id),
        }
        return columns[entity_type]

    @staticmethod
    def _build_sync_statement(entity_type: str, user_id: int) -> Select[Any] | None:
        if entity_type == "user":
            return select(
                User.telegram_user_id,
                User.username,
                User.is_owner,
                User.preferences_json,
                User.server_version,
                User.updated_at,
                User.created_at,
            ).where(User.telegram_user_id == user_id)
        if entity_type == "request":
            return select(
                Request.id,
                Request.type,
                Request.status,
                Request.correlation_id,
                Request.input_url,
                Request.normalized_url,
                Request.dedupe_hash,
                Request.lang_detected,
                Request.server_version,
                Request.is_deleted,
                Request.deleted_at,
                Request.updated_at,
                Request.created_at,
            ).where(Request.user_id == user_id)
        if entity_type == "summary":
            return (
                select(
                    Summary.id,
                    Summary.request_id,
                    Summary.lang,
                    Summary.json_payload,
                    Summary.version,
                    Summary.server_version,
                    Summary.is_read,
                    Summary.is_favorited,
                    Summary.is_deleted,
                    Summary.deleted_at,
                    Summary.updated_at,
                    Summary.created_at,
                )
                .join(Request, Summary.request_id == Request.id)
                .where(Request.user_id == user_id)
            )
        if entity_type == "crawl_result":
            return (
                select(
                    CrawlResult.id,
                    CrawlResult.request_id,
                    CrawlResult.source_url,
                    CrawlResult.endpoint,
                    CrawlResult.http_status,
                    CrawlResult.metadata_json,
                    CrawlResult.latency_ms,
                    CrawlResult.server_version,
                    CrawlResult.is_deleted,
                    CrawlResult.deleted_at,
                    CrawlResult.updated_at,
                )
                .join(Request, CrawlResult.request_id == Request.id)
                .where(Request.user_id == user_id)
            )
        if entity_type == "llm_call":
            return (
                select(
                    LLMCall.id,
                    LLMCall.request_id,
                    LLMCall.provider,
                    LLMCall.model,
                    LLMCall.status,
                    LLMCall.tokens_prompt,
                    LLMCall.tokens_completion,
                    LLMCall.cost_usd,
                    LLMCall.server_version,
                    LLMCall.is_deleted,
                    LLMCall.deleted_at,
                    LLMCall.updated_at,
                    LLMCall.created_at,
                )
                .join(Request, LLMCall.request_id == Request.id)
                .where(Request.user_id == user_id)
            )
        if entity_type == "highlight":
            return select(
                SummaryHighlight.id,
                SummaryHighlight.summary_id,
                SummaryHighlight.text,
                SummaryHighlight.start_offset,
                SummaryHighlight.end_offset,
                SummaryHighlight.color,
                SummaryHighlight.note,
                SummaryHighlight.server_version,
                SummaryHighlight.updated_at,
                SummaryHighlight.created_at,
            ).where(SummaryHighlight.user_id == user_id)
        if entity_type == "tag":
            return select(
                Tag.id,
                Tag.name,
                Tag.normalized_name,
                Tag.color,
                Tag.server_version,
                Tag.is_deleted,
                Tag.deleted_at,
                Tag.updated_at,
                Tag.created_at,
            ).where(Tag.user_id == user_id)
        if entity_type == "summary_tag":
            return (
                select(
                    SummaryTag.id,
                    SummaryTag.summary_id,
                    SummaryTag.tag_id,
                    SummaryTag.source,
                    SummaryTag.server_version,
                    SummaryTag.created_at,
                )
                .join(Summary, SummaryTag.summary_id == Summary.id)
                .join(Request, Summary.request_id == Request.id)
                .where(Request.user_id == user_id)
            )
        return None

    async def get_highlights_for_user(
        self, user_id: int, *, since: int = 0
    ) -> list[dict[str, Any]]:
        stmt = select(SummaryHighlight).where(SummaryHighlight.user_id == user_id)
        if since > 0:
            # Incremental sync cursor pushed to the DB so a poll skips already-synced
            # rows instead of re-reading the whole history (audit #2).
            stmt = stmt.where(SummaryHighlight.server_version > since)
        async with self._database.session() as session:
            rows = (await session.execute(stmt)).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def get_tags_for_user(self, user_id: int, *, since: int = 0) -> list[dict[str, Any]]:
        stmt = select(Tag).where(Tag.user_id == user_id)
        if since > 0:
            stmt = stmt.where(Tag.server_version > since)
        async with self._database.session() as session:
            rows = (await session.execute(stmt)).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def get_summary_tags_for_user(
        self, user_id: int, *, since: int = 0
    ) -> list[dict[str, Any]]:
        stmt = (
            select(SummaryTag)
            .join(Summary, SummaryTag.summary_id == Summary.id)
            .join(Request, Summary.request_id == Request.id)
            .where(Request.user_id == user_id)
        )
        if since > 0:
            stmt = stmt.where(SummaryTag.server_version > since)
        async with self._database.session() as session:
            rows = (await session.execute(stmt)).scalars()
            return [model_to_dict(row) or {} for row in rows]
