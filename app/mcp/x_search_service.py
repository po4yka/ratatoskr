"""MCP x_bookmarks search service.

Serves the ``x_search`` MCP tool via Postgres full-text search over ``x_bookmark_metadata.tweet_text_tsv``. Never spawns the host-side ``ft`` binary — design decision DEC-001b mandates Postgres-only reads (see ``docs/explanation/x-bookmarks-integration.md`` "Why Postgres FTS instead of ``ft search`` subprocess").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.db.models.core import XCategory
from app.mcp.helpers import McpErrorResult, isotime

logger = logging.getLogger("ratatoskr.mcp")

if TYPE_CHECKING:
    from app.mcp.context import McpServerContext


_VALID_CATEGORY_VALUES: frozenset[str] = frozenset(member.value for member in XCategory)


class XSearchService:
    """Postgres-backed full-text search for ingested x_bookmarks bookmarks."""

    def __init__(self, context: McpServerContext) -> None:
        self.context = context

    async def search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any] | McpErrorResult:
        """Search ingested x_bookmarks bookmarks by full-text relevance.

        Returns the top ``limit`` matches ranked by ``ts_rank_cd`` over the ``tweet_text_tsv`` GIN-indexed column, joined to ``requests`` for the canonical URL. ``category`` (when provided) must be a valid ``XCategory`` value or an MCP error result is returned without touching the database.
        """
        from app.db.models import Request, XBookmarkMetadata

        clamped_limit = max(1, min(50, int(limit)))
        query_text = query.strip()
        if not query_text:
            return {"results": [], "query": query, "category": category}

        if category is not None and category not in _VALID_CATEGORY_VALUES:
            return {
                "error": (
                    f"Invalid category '{category}'. "
                    f"Expected one of: {sorted(_VALID_CATEGORY_VALUES)}"
                )
            }

        try:
            runtime = self.context.ensure_runtime()
            async with runtime.database.session() as session:
                ts_query = func.plainto_tsquery("english", query_text)
                rank = func.ts_rank_cd(XBookmarkMetadata.tweet_text_tsv, ts_query)
                stmt = (
                    select(
                        Request.id.label("request_id"),
                        Request.normalized_url.label("canonical_url"),
                        XBookmarkMetadata.x_category.label("category"),
                        XBookmarkMetadata.tweet_text,
                        XBookmarkMetadata.tweet_author,
                        XBookmarkMetadata.posted_at,
                        rank.label("rank"),
                    )
                    .join(
                        XBookmarkMetadata,
                        XBookmarkMetadata.request_id == Request.id,
                    )
                    .where(
                        XBookmarkMetadata.tweet_text_tsv.op("@@")(ts_query),
                        *self.context.request_scope_filters(Request),
                    )
                    .order_by(
                        rank.desc(),
                        XBookmarkMetadata.posted_at.desc().nullslast(),
                    )
                    .limit(clamped_limit)
                )
                if category is not None:
                    stmt = stmt.where(XBookmarkMetadata.x_category == category)

                rows = (await session.execute(stmt)).all()

            results = [
                {
                    "request_id": int(row.request_id),
                    "canonical_url": row.canonical_url or "",
                    "category": row.category,
                    "tweet_text": row.tweet_text or "",
                    "tweet_author": row.tweet_author or "",
                    "posted_at": isotime(row.posted_at),
                    "rank": float(row.rank or 0.0),
                }
                for row in rows
            ]
            return {
                "results": results,
                "query": query,
                "category": category,
            }
        except Exception as exc:
            logger.exception("x_search failed")
            return {"error": str(exc), "query": query, "category": category}
