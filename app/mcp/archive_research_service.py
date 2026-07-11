"""MCP adapter for citation-first research across a scoped personal archive."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from app.application.dto.vector_search import EntityType, RetrievalScope
from app.application.services.archive_research import (
    ArchiveEvidence,
    CitationFirstArchiveResearchGraph,
)
from app.core.logging_utils import generate_correlation_id

if TYPE_CHECKING:
    from app.mcp.article_service import ArticleReadService
    from app.mcp.context import McpServerContext
    from app.mcp.x_search_service import XSearchService

logger = logging.getLogger("ratatoskr.mcp")


class ArchiveResearchMcpService:
    """Supply scoped archive evidence to the bounded citation-first graph."""

    def __init__(
        self,
        context: McpServerContext,
        article_service: ArticleReadService,
        x_search_service: XSearchService,
    ) -> None:
        self._context = context
        self._article_service = article_service
        self._x_search_service = x_search_service

    async def research(self, query: str, max_sources: int = 12) -> dict[str, Any]:
        """Research a question from archive evidence and return verified citations."""
        correlation_id = generate_correlation_id()
        try:
            return await CitationFirstArchiveResearchGraph(self).run(query, max_sources=max_sources)
        except Exception:
            logger.exception("archive_research_failed", extra={"correlation_id": correlation_id})
            return {
                "error": f"Archive research failed. Error ID: {correlation_id}",
                "correlation_id": correlation_id,
            }

    async def retrieve(self, *, query: str, per_source_limit: int) -> list[ArchiveEvidence]:
        groups = await asyncio.gather(
            self._retrieve_unified(query, per_source_limit),
            self._retrieve_article_summaries(query, per_source_limit),
            self._retrieve_x_bookmarks(query, per_source_limit),
            self._retrieve_annotations(query, per_source_limit),
        )
        return [item for group in groups for item in group]

    async def hydrate(self, evidence: ArchiveEvidence) -> ArchiveEvidence:
        if evidence.source_kind != "summary":
            return evidence
        summary_id = _numeric_suffix(evidence.source_id)
        if summary_id is None:
            return replace(evidence, excerpt="")
        detail = await self._article_service.get_article(summary_id)
        if "error" in detail:
            return replace(evidence, excerpt="")
        excerpt = _first_text(
            detail.get("summary_1000"),
            detail.get("summary_250"),
            detail.get("tldr"),
            evidence.excerpt,
        )
        return replace(
            evidence,
            title=str(detail.get("title") or evidence.title),
            excerpt=excerpt,
            url=str(detail.get("url") or evidence.url or "") or None,
        )

    async def _retrieve_unified(self, query: str, limit: int) -> list[ArchiveEvidence]:
        """Use the shared retrieval adapter for summary, repo, and mirror discovery."""
        runtime = self._context.ensure_runtime()
        vector_service = await self._context.init_vector_service()
        resources = runtime.vector_state.resources
        if vector_service is None or len(resources) != 2 or runtime.cfg is None:
            return []
        vector_store, embedding_service = resources
        from app.infrastructure.retrieval.qdrant_retrieval_adapter import QdrantRetrievalAdapter

        scope = RetrievalScope(
            environment=runtime.cfg.vector_store.environment,
            user_scope=runtime.cfg.vector_store.user_scope,
            user_id=self._context.user_id,
        )
        adapter = QdrantRetrievalAdapter(
            vector_store=vector_store,
            embedding_service=embedding_service,
            db=runtime.database,
        )
        entity_types = [EntityType.SUMMARY]
        if scope.user_id is not None:
            entity_types.extend((EntityType.REPOSITORY, EntityType.GIT_MIRROR))
        results = await asyncio.gather(
            *(
                adapter.retrieve(entity_type=entity_type, scope=scope, query=query, top_k=limit)
                for entity_type in entity_types
            ),
            return_exceptions=True,
        )
        evidence: list[ArchiveEvidence] = []
        for entity_type, result in zip(entity_types, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                logger.warning(
                    "archive_research_retrieval_failed",
                    extra={"entity_type": entity_type.value, "error": str(result)},
                )
                continue
            if isinstance(result, BaseException):
                raise result
            for hit in result.hits:
                data = hit.hydrated or hit.payload
                title = _first_text(
                    data.get("title"), data.get("full_name"), data.get("name"), hit.entity_id
                )
                excerpt = _first_text(
                    data.get("summary_250"),
                    data.get("description"),
                    data.get("readme_excerpt"),
                    data.get("clone_url"),
                )
                url = _first_text(data.get("url"), data.get("clone_url")) or None
                evidence.append(
                    ArchiveEvidence(
                        source_kind=entity_type.value,
                        source_id=f"{entity_type.value}:{hit.entity_id}",
                        title=title,
                        excerpt=excerpt,
                        url=url,
                        score=hit.score,
                    )
                )
        return evidence

    async def _retrieve_x_bookmarks(self, query: str, limit: int) -> list[ArchiveEvidence]:
        response = await self._x_search_service.search(query, limit=limit)
        if "error" in response:
            return []
        return [
            ArchiveEvidence(
                source_kind="x_bookmark",
                source_id=f"x_bookmark:{row['request_id']}",
                title=_first_text(row.get("tweet_author"), "X bookmark"),
                excerpt=_first_text(row.get("tweet_text")),
                url=_first_text(row.get("canonical_url")) or None,
                score=float(row.get("rank") or 0.0),
            )
            for row in response.get("results", [])
        ]

    async def _retrieve_article_summaries(self, query: str, limit: int) -> list[ArchiveEvidence]:
        """Add bounded lexical summary matches beside semantic vector discovery."""
        response = await self._article_service.search_articles(query, limit=limit)
        if "error" in response:
            return []
        return [
            ArchiveEvidence(
                source_kind="summary",
                source_id=f"summary:{row['summary_id']}",
                title=_first_text(row.get("title"), f"Summary {row['summary_id']}"),
                excerpt=_first_text(row.get("summary_250"), row.get("tldr")),
                url=_first_text(row.get("url")) or None,
                score=0.0,
            )
            for row in response.get("results", [])
            if row.get("summary_id") is not None
        ]

    async def _retrieve_annotations(self, query: str, limit: int) -> list[ArchiveEvidence]:
        user_id = self._context.user_id
        if user_id is None:
            return []
        from app.db.models import Request, Summary, SummaryHighlight

        terms = _annotation_terms(query)
        if not terms:
            return []
        runtime = self._context.ensure_runtime()
        async with runtime.database.session() as session:
            rows = (
                await session.execute(
                    select(SummaryHighlight, Summary, Request)
                    .join(Summary, SummaryHighlight.summary_id == Summary.id)
                    .join(Request, Summary.request_id == Request.id)
                    .where(
                        SummaryHighlight.user_id == user_id,
                        Request.user_id == user_id,
                        Summary.is_deleted.is_(False),
                        or_(
                            *(
                                or_(
                                    SummaryHighlight.text.ilike(f"%{term}%"),
                                    SummaryHighlight.note.ilike(f"%{term}%"),
                                )
                                for term in terms
                            )
                        ),
                    )
                    .order_by(SummaryHighlight.updated_at.desc())
                    .limit(limit)
                )
            ).all()
        evidence: list[ArchiveEvidence] = []
        for highlight, summary, request in rows:
            summary_title = str(summary.title or f"Summary {summary.id}")
            url = request.normalized_url or request.input_url
            evidence.append(
                ArchiveEvidence(
                    source_kind="highlight",
                    source_id=f"highlight:{highlight.id}",
                    title=summary_title,
                    excerpt=str(highlight.text or ""),
                    url=url,
                    score=1.0,
                )
            )
            if highlight.note:
                evidence.append(
                    ArchiveEvidence(
                        source_kind="note",
                        source_id=f"note:{highlight.id}",
                        title=summary_title,
                        excerpt=str(highlight.note),
                        url=url,
                        score=1.0,
                    )
                )
        return evidence


def _numeric_suffix(source_id: str) -> int | None:
    try:
        return int(source_id.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def _first_text(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _annotation_terms(query: str) -> list[str]:
    """Return bounded literal terms for user-owned annotation matching."""
    return list(dict.fromkeys(re.findall(r"[\w-]{3,}", query.lower())))[:8]
