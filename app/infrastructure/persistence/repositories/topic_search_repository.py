"""SQLAlchemy implementation of the topic search repository."""

from __future__ import annotations

import operator
from functools import reduce
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from app.application.services.topic_search_utils import (
    TopicSearchDocument,
    build_snippet,
    clean_snippet,
    compose_search_body,
    ensure_mapping,
    normalize_text,
)
from app.core.logging_utils import get_logger
from app.db.models import Request, Summary, SummaryTag, Tag, TopicSearchIndex
from app.db.topic_search_manager import TopicSearchIndexManager

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.db.session import Database

logger = get_logger(__name__)


class TopicSearchRepositoryAdapter:
    """Adapter for topic search index operations."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._manager = TopicSearchIndexManager(database, logger)

    async def async_ensure_index(self) -> None:
        """Ensure the denormalized topic search rows are synchronized."""
        await self._manager.ensure_index()

    async def async_write_document(self, document: TopicSearchDocument) -> None:
        """Write a document to the search index."""
        async with self._database.transaction() as session:
            await self._write_document(session, document)

    async def async_refresh_index(self, request_id: int) -> None:
        """Refresh index for a specific request."""
        await self._manager.refresh_index(request_id)

    async def async_update_tags_for_summary(self, summary_id: int) -> None:
        """Merge active user tags into a summary's topic search row."""
        async with self._database.transaction() as session:
            summary = await session.get(Summary, summary_id)
            if summary is None:
                return
            index_row = await session.get(TopicSearchIndex, summary.request_id)
            if index_row is None:
                return

            tag_names = list(
                await session.scalars(
                    select(Tag.name)
                    .join(SummaryTag, SummaryTag.tag_id == Tag.id)
                    .where(SummaryTag.summary_id == summary_id, Tag.is_deleted.is_(False))
                    .order_by(Tag.name.asc())
                )
            )
            merged_tags = self._merge_tags(index_row.tags or "", tag_names)
            if merged_tags != (index_row.tags or ""):
                await session.execute(
                    update(TopicSearchIndex)
                    .where(TopicSearchIndex.request_id == summary.request_id)
                    .values(tags=merged_tags)
                )

    async def async_rebuild_index(self) -> None:
        """Rebuild the entire search index."""
        await self._manager.ensure_index()

    async def async_reset_index(self) -> None:
        """Clear and rebuild the topic search index."""
        await self._manager.ensure_index()

    async def async_search_request_ids(
        self, topic: str, *, candidate_limit: int = 100
    ) -> list[int] | None:
        """Find request IDs matching the topic using PostgreSQL full-text search."""
        return await self._manager.find_request_ids(topic, candidate_limit=candidate_limit)

    async def async_fts_search_paginated(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        user_id: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Perform PostgreSQL full-text search with pagination."""
        search_query = query.strip()
        if not search_query:
            return [], 0

        params: dict[str, Any] = {
            "query": search_query,
            "limit": limit,
            "offset": offset,
            "user_id": user_id,
        }
        count_sql_lines = [
            "SELECT COUNT(*)",
            "FROM topic_search_index AS t",
            "JOIN requests AS r ON t.request_id = r.id",
            "WHERE t.body_tsv @@ websearch_to_tsquery('simple', :query)",
        ]
        search_sql_lines = [
            "SELECT t.request_id, t.title, t.snippet, t.source, t.published_at",
            "FROM topic_search_index AS t",
            "JOIN requests AS r ON t.request_id = r.id",
            "WHERE t.body_tsv @@ websearch_to_tsquery('simple', :query)",
        ]
        if user_id is not None:
            user_filter = "AND r.user_id = :user_id"
            count_sql_lines.append(user_filter)
            search_sql_lines.append(user_filter)
        search_sql_lines.extend(
            [
                "ORDER BY ts_rank_cd(t.body_tsv, websearch_to_tsquery('simple', :query)) DESC,",
                "         t.request_id DESC",
                "LIMIT :limit OFFSET :offset",
            ]
        )
        count_sql = text("\n".join(count_sql_lines))
        search_sql = text("\n".join(search_sql_lines))
        async with self._database.session() as session:
            total = int((await session.execute(count_sql, params)).scalar_one() or 0)
            rows = await session.execute(search_sql, params)
            results = [
                {
                    "request_id": row.request_id,
                    "title": row.title,
                    "snippet": row.snippet,
                    "source": row.source,
                    "published_at": row.published_at,
                }
                for row in rows
            ]
        return results, total

    async def async_search_documents(
        self,
        topic: str,
        limit: int = 10,
    ) -> list[TopicSearchDocument]:
        """Search documents using the PostgreSQL topic search index."""
        search_query = topic.strip()
        if not search_query or limit <= 0:
            return []

        ts_query = func.websearch_to_tsquery("simple", search_query)
        rank = func.ts_rank_cd(TopicSearchIndex.body_tsv, ts_query)
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(TopicSearchIndex)
                    .where(TopicSearchIndex.body_tsv.op("@@")(ts_query))
                    .order_by(rank.desc(), TopicSearchIndex.request_id.desc())
                    .limit(max(limit * 5, 25))
                )
            ).scalars()

            documents: list[TopicSearchDocument] = []
            seen_urls: set[str] = set()
            for row in rows:
                url = normalize_text(row.url)
                if not url or url in seen_urls:
                    continue
                documents.append(
                    TopicSearchDocument(
                        request_id=row.request_id,
                        url=url,
                        title=normalize_text(row.title) or url,
                        snippet=clean_snippet(row.snippet),
                        source=normalize_text(row.source),
                        published_at=normalize_text(row.published_at),
                        body=row.body or "",
                        tags_text=row.tags or "",
                    )
                )
                seen_urls.add(url)
                if len(documents) >= limit:
                    break
            return documents

    async def async_scan_documents(
        self,
        terms: Sequence[str],
        normalized_query: str,
        seen_urls: set[str],
        limit: int,
        max_scan: int | None = None,
    ) -> list[TopicSearchDocument]:
        """Scan summaries for documents matching terms as a fallback path."""
        if limit <= 0:
            return []

        stmt = (
            select(Summary, Request)
            .join(Request, Summary.request_id == Request.id)
            .where(Summary.json_payload.is_not(None))
            .order_by(Summary.created_at.desc())
        )
        if terms:
            term_conditions = []
            for term in terms:
                pattern = f"%{term}%"
                term_conditions.extend(
                    [
                        Request.content_text.ilike(pattern),
                        Request.normalized_url.ilike(pattern),
                        Request.input_url.ilike(pattern),
                    ]
                )
            if term_conditions:
                stmt = stmt.where(reduce(operator.or_, term_conditions))
        if max_scan:
            stmt = stmt.limit(max_scan)

        async with self._database.session() as session:
            rows = await session.execute(stmt)
            documents: list[TopicSearchDocument] = []
            for summary, request in rows:
                payload = ensure_mapping(summary.json_payload)
                metadata = ensure_mapping(payload.get("metadata"))
                url = (
                    normalize_text(metadata.get("canonical_url"))
                    or normalize_text(metadata.get("url"))
                    or normalize_text(request.normalized_url)
                    or normalize_text(request.input_url)
                )
                if not url or url in seen_urls:
                    continue

                title = (
                    normalize_text(metadata.get("title"))
                    or normalize_text(payload.get("title"))
                    or url
                )
                haystack, tags_text = compose_search_body(
                    title=title,
                    payload=payload,
                    metadata=metadata,
                    content_text=request.content_text,
                )
                if not self._matches(terms, normalized_query, haystack):
                    continue

                documents.append(
                    TopicSearchDocument(
                        request_id=request.id,
                        url=url,
                        title=title,
                        snippet=build_snippet(payload),
                        source=normalize_text(metadata.get("domain") or metadata.get("source")),
                        published_at=normalize_text(
                            metadata.get("published_at")
                            or metadata.get("published")
                            or metadata.get("last_updated")
                        ),
                        body=haystack,
                        tags_text=tags_text,
                    )
                )
                seen_urls.add(url)
                if len(documents) >= limit:
                    break
            return documents

    async def _write_document(self, session: Any, document: TopicSearchDocument) -> None:
        stmt = insert(TopicSearchIndex).values(
            request_id=document.request_id,
            url=document.url,
            title=document.title,
            snippet=document.snippet,
            source=document.source,
            published_at=document.published_at,
            body=document.body,
            tags=document.tags_text,
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[TopicSearchIndex.request_id],
                set_={
                    "url": stmt.excluded.url,
                    "title": stmt.excluded.title,
                    "snippet": stmt.excluded.snippet,
                    "source": stmt.excluded.source,
                    "published_at": stmt.excluded.published_at,
                    "body": stmt.excluded.body,
                    "tags": stmt.excluded.tags,
                },
            )
        )

    @staticmethod
    def _merge_tags(existing_tags: str, tag_names: Sequence[str]) -> str:
        merged_parts: list[str] = []
        seen: set[str] = set()
        for part in [*existing_tags.split(), *tag_names]:
            text_value = str(part).strip()
            if not text_value:
                continue
            lower = text_value.lower()
            if lower in seen:
                continue
            merged_parts.append(text_value)
            seen.add(lower)
        return " ".join(merged_parts)

    @staticmethod
    def _matches(terms: Sequence[str], normalized_query: str, haystack: str) -> bool:
        if not haystack:
            return False
        if not terms:
            return normalized_query in haystack
        return all(term in haystack for term in terms)
