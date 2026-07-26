"""MCP tool registration adapters — thin wrappers required by the MCP framework.

Each function is a single-line adapter: wrap the service call, serialize the
result to JSON for the wire protocol, and register it through a schema-testable
contribution. No domain logic lives here; all business logic is in the injected
service classes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable  # noqa: TC003
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.api.local_rate_limiter import LocalRateLimiter
from app.mcp.helpers import to_json
from app.observability.metrics import record_request


def _env_positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


# In-process per-(tool, tenant) rate limiting for MCP. The Telegram
# UserRateLimiter is not wired to the MCP transport, so without this a single
# caller could drive unbounded scrape + LLM cost via tools like
# create_aggregation_bundle (CWE-770). The bucket is keyed by tool AND the
# effective request identity: in the hosted multi-tenant JWT mode
# (MCP_AUTH_MODE=jwt over SSE, one process serving many authenticated users) a
# tool-name-only key would let every tenant share one global budget, so one
# caller could starve all others. Still process-local -- a horizontally-scaled
# deployment needs a shared (Redis) limiter, tracked separately.
_MCP_TOOL_RATE_LIMITER = LocalRateLimiter()
_MCP_TOOL_WINDOW_SEC = _env_positive_int("MCP_TOOL_RATE_WINDOW_SEC", 60)
_MCP_TOOL_DEFAULT_LIMIT = _env_positive_int("MCP_TOOL_RATE_LIMIT", 60)
_MCP_EXPENSIVE_TOOL_LIMIT = _env_positive_int("MCP_EXPENSIVE_TOOL_RATE_LIMIT", 5)
# Tools that trigger a billed external-provider call on every invocation and must
# be capped tighter than the default read tier. Two cost shapes qualify:
#   - scrape + LLM fan-out: create_aggregation_bundle, promote_to_library.
#   - a per-call embedding-provider request: semantic_search, hybrid_search, and
#     find_similar_articles all embed their query through the vector/local
#     embedding service on every call (find_similar_articles re-embeds the source
#     summary's seed text rather than reusing its stored vector), so at the default
#     limit a single caller could drive unbounded embedding cost (CWE-770).
_MCP_EXPENSIVE_TOOLS = frozenset(
    {
        "create_aggregation_bundle",
        "promote_to_library",
        "semantic_search",
        "hybrid_search",
        "find_similar_articles",
    }
)


def _mcp_identity_key(context: Any) -> str:
    """Resolve a per-tenant rate-limit sub-key from the active request identity.

    In hosted JWT mode this is the authenticated user (or client_id) resolved
    per request; in stdio/local mode it collapses to a single stable key (one
    user), so single-user behavior is unchanged.
    """
    user_id = getattr(context, "user_id", None)
    if user_id is not None:
        return f"u{user_id}"
    client_id = getattr(context, "client_id", None)
    if client_id:
        return f"c{client_id}"
    return "anon"


def _mcp_tool_rate_limited(tool_name: str, identity_key: str) -> bool:
    limit = (
        _MCP_EXPENSIVE_TOOL_LIMIT if tool_name in _MCP_EXPENSIVE_TOOLS else _MCP_TOOL_DEFAULT_LIMIT
    )
    # LocalRateLimiter splits its internal key on ":" and reads the last segment
    # as the window bucket, so extra ":"-separated identity components are safe.
    bucket_key = f"{tool_name}:{identity_key}"
    allowed, _ = _MCP_TOOL_RATE_LIMITER.check(bucket_key, limit=limit, window=_MCP_TOOL_WINDOW_SEC)
    return not allowed


def mcp_rate_limit_exceeded(operation_name: str, context: Any) -> bool:
    """Per-(operation, tenant) MCP rate-limit check shared by tools AND resources.

    Resources register through this SAME limiter/buckets so they can no longer bypass
    the tool-layer limiter (they previously routed straight to their service with no
    cap, letting a caller drive unbounded DB / vector-scan reads -- CWE-770). Keyed
    by the operation name plus the effective request identity, so a resource and a
    tool never share a bucket and each tenant keeps an isolated budget.
    """
    return _mcp_tool_rate_limited(operation_name, _mcp_identity_key(context))


if TYPE_CHECKING:
    from app.mcp.aggregation_service import AggregationMcpService
    from app.mcp.archive_research_service import ArchiveResearchMcpService
    from app.mcp.article_service import ArticleReadService
    from app.mcp.catalog_service import CatalogReadService
    from app.mcp.context import McpServerContext
    from app.mcp.semantic_service import SemanticSearchService
    from app.mcp.signal_service import SignalMcpService
    from app.mcp.x_search_service import XSearchService


class McpToolRegistrar(Protocol):
    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Return a FastMCP-compatible tool decorator."""
        ...


class McpToolContribution(BaseModel):
    """Schema-testable MCP tool contribution."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    handler: Callable[..., Any] = Field(exclude=True)
    description: str

    @classmethod
    def from_handler(cls, handler: Callable[..., Any]) -> McpToolContribution:
        return cls(
            name=handler.__name__,
            handler=handler,
            description=(handler.__doc__ or "").strip(),
        )

    def register(self, mcp: McpToolRegistrar) -> None:
        mcp.tool()(self.handler)


def _contribute_tool(
    contributions: list[McpToolContribution],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        contributions.append(McpToolContribution.from_handler(fn))
        return fn

    return decorator


def register_tools(
    mcp: Any,
    *,
    context: McpServerContext,
    aggregation_service: AggregationMcpService,
    article_service: ArticleReadService,
    catalog_service: CatalogReadService,
    semantic_service: SemanticSearchService,
    signal_service: SignalMcpService | None = None,
    x_search_service_inst: XSearchService | None = None,
    archive_research_service: ArchiveResearchMcpService | None = None,
) -> None:
    signal_runtime: Any = signal_service if signal_service is not None else _NullSignalService()
    x_search_runtime: Any = (
        x_search_service_inst if x_search_service_inst is not None else _NullXSearchService()
    )
    archive_research_runtime: Any = (
        archive_research_service
        if archive_research_service is not None
        else _NullArchiveResearchService()
    )
    contributions: list[McpToolContribution] = []
    contribute_tool = _contribute_tool(contributions)

    def _status_from_result(result: Any) -> str:
        return "error" if isinstance(result, dict) and "error" in result else "success"

    def _record_tool_metric(tool_name: str, *, status: str, started_at: float) -> None:
        record_request(
            request_type=tool_name,
            status=status,
            source="mcp",
            latency_seconds=max(0.0, time.perf_counter() - started_at),
        )

    def _rate_limited_result(tool_name: str) -> dict[str, str]:
        started_at = time.perf_counter()
        _record_tool_metric(tool_name, status="error", started_at=started_at)
        return {
            "error": "rate_limited",
            "message": f"MCP tool '{tool_name}' rate limit exceeded; retry later.",
        }

    async def _call_async(tool_name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        if _mcp_tool_rate_limited(tool_name, _mcp_identity_key(context)):
            return _rate_limited_result(tool_name)
        started_at = time.perf_counter()
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            _record_tool_metric(tool_name, status="error", started_at=started_at)
            raise
        _record_tool_metric(tool_name, status=_status_from_result(result), started_at=started_at)
        return result

    def _call_sync(tool_name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        if _mcp_tool_rate_limited(tool_name, _mcp_identity_key(context)):
            return _rate_limited_result(tool_name)
        started_at = time.perf_counter()
        try:
            result = fn(*args, **kwargs)
        except Exception:
            _record_tool_metric(tool_name, status="error", started_at=started_at)
            raise
        _record_tool_metric(tool_name, status=_status_from_result(result), started_at=started_at)
        return result

    @contribute_tool
    async def create_aggregation_bundle(
        items: list[dict[str, Any]],
        lang_preference: str = "auto",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create and run an aggregation bundle for the scoped MCP user."""
        return to_json(
            await _call_async(
                "create_aggregation_bundle",
                aggregation_service.create_aggregation_bundle,
                items=items,
                lang_preference=lang_preference,
                metadata=metadata,
            )
        )

    @contribute_tool
    async def get_aggregation_bundle(session_id: int) -> str:
        """Get one persisted aggregation bundle by session ID."""
        return to_json(
            await _call_async(
                "get_aggregation_bundle",
                aggregation_service.get_aggregation_bundle,
                session_id,
            )
        )

    @contribute_tool
    async def list_aggregation_bundles(
        limit: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> str:
        """List aggregation bundles for the scoped MCP user."""
        return to_json(
            await _call_async(
                "list_aggregation_bundles",
                aggregation_service.list_aggregation_bundles,
                limit=limit,
                offset=offset,
                status=status,
            )
        )

    @contribute_tool
    def check_source_supported(url: str, source_kind_hint: str | None = None) -> str:
        """Classify whether a URL fits the public aggregation source contract."""
        return to_json(
            _call_sync(
                "check_source_supported",
                aggregation_service.check_source_supported,
                url=url,
                source_kind_hint=source_kind_hint,
            )
        )

    @contribute_tool
    async def search_articles(query: str, limit: int = 10) -> str:
        """Search stored article summaries by keyword, topic, or entity."""
        return to_json(
            await _call_async("search_articles", article_service.search_articles, query, limit)
        )

    @contribute_tool
    async def get_article(summary_id: int) -> str:
        """Get full details of an article summary by its ID."""
        return to_json(await _call_async("get_article", article_service.get_article, summary_id))

    @contribute_tool
    async def list_articles(
        limit: int = 20,
        offset: int = 0,
        is_favorited: bool | None = None,
        lang: str | None = None,
        tag: str | None = None,
    ) -> str:
        """List stored article summaries with optional filters."""
        return to_json(
            await _call_async(
                "list_articles",
                article_service.list_articles,
                limit,
                offset,
                is_favorited,
                lang,
                tag,
            )
        )

    @contribute_tool
    async def get_article_content(summary_id: int) -> str:
        """Get the full extracted content (markdown/text) of an article."""
        return to_json(
            await _call_async(
                "get_article_content", article_service.get_article_content, summary_id
            )
        )

    @contribute_tool
    async def get_stats() -> str:
        """Get statistics about the Ratatoskr article database."""
        return to_json(await _call_async("get_stats", article_service.get_stats))

    @contribute_tool
    async def find_by_entity(
        entity_name: str, entity_type: str | None = None, limit: int = 10
    ) -> str:
        """Find articles that mention a specific entity."""
        return to_json(
            await _call_async(
                "find_by_entity", article_service.find_by_entity, entity_name, entity_type, limit
            )
        )

    @contribute_tool
    async def x_search(
        query: str,
        category: str | None = None,
        limit: int = 10,
    ) -> str:
        """Search ingested x_bookmarks bookmarks via Postgres full-text search."""
        return to_json(
            await _call_async(
                "x_search",
                x_search_runtime.search,
                query,
                category,
                limit,
            )
        )

    @contribute_tool
    async def ask_my_archive(query: str, max_sources: int = 12) -> str:
        """Research a question from the scoped archive with verified citations."""
        return to_json(
            await _call_async(
                "ask_my_archive",
                archive_research_runtime.research,
                query,
                max_sources,
            )
        )

    @contribute_tool
    async def list_collections(limit: int = 20, offset: int = 0) -> str:
        """List article collections (folders/reading lists)."""
        return to_json(
            await _call_async("list_collections", catalog_service.list_collections, limit, offset)
        )

    @contribute_tool
    async def get_collection(
        collection_id: int, include_items: bool = True, limit: int = 50
    ) -> str:
        """Get details of a specific collection and its article summaries."""
        return to_json(
            await _call_async(
                "get_collection",
                catalog_service.get_collection,
                collection_id,
                include_items,
                limit,
            )
        )

    @contribute_tool
    async def list_videos(limit: int = 20, offset: int = 0, status: str | None = None) -> str:
        """List downloaded YouTube videos with metadata."""
        return to_json(
            await _call_async("list_videos", catalog_service.list_videos, limit, offset, status)
        )

    @contribute_tool
    async def get_video_transcript(video_id: str) -> str:
        """Get the transcript text of a downloaded YouTube video."""
        return to_json(
            await _call_async(
                "get_video_transcript", catalog_service.get_video_transcript, video_id
            )
        )

    @contribute_tool
    async def check_url(url: str) -> str:
        """Check if a URL has already been processed and summarised."""
        return to_json(await _call_async("check_url", article_service.check_url, url))

    @contribute_tool
    async def semantic_search(
        description: str,
        limit: int = 10,
        language: str | None = None,
        min_similarity: float = 0.25,
        rerank: bool = False,
        include_chunks: bool = True,
    ) -> str:
        """Search articles by semantic meaning with resilient fallback strategy."""
        return to_json(
            await _call_async(
                "semantic_search",
                semantic_service.semantic_search,
                description,
                limit=limit,
                language=language,
                min_similarity=min_similarity,
                rerank=rerank,
                include_chunks=include_chunks,
            )
        )

    @contribute_tool
    async def hybrid_search(
        query: str,
        limit: int = 10,
        language: str | None = None,
        min_similarity: float = 0.25,
        rerank: bool = False,
    ) -> str:
        """Combine keyword and semantic retrieval into a single ranked result list."""
        return to_json(
            await _call_async(
                "hybrid_search",
                semantic_service.hybrid_search,
                query,
                limit=limit,
                language=language,
                min_similarity=min_similarity,
                rerank=rerank,
            )
        )

    @contribute_tool
    async def find_similar_articles(
        summary_id: int,
        limit: int = 10,
        min_similarity: float = 0.3,
        rerank: bool = False,
        include_chunks: bool = True,
    ) -> str:
        """Find articles semantically similar to an existing summary."""
        return to_json(
            await _call_async(
                "find_similar_articles",
                semantic_service.find_similar_articles,
                summary_id,
                limit=limit,
                min_similarity=min_similarity,
                rerank=rerank,
                include_chunks=include_chunks,
            )
        )

    @contribute_tool
    async def list_signal_sources(limit: int = 50) -> str:
        """List signal sources for the scoped MCP user."""
        return to_json(await _call_async("list_signal_sources", signal_runtime.list_sources, limit))

    @contribute_tool
    async def list_user_signals(limit: int = 20, status: str | None = None) -> str:
        """List scored signal candidates for the scoped MCP user."""
        return to_json(
            await _call_async("list_user_signals", signal_runtime.list_signals, limit, status)
        )

    @contribute_tool
    async def update_signal_feedback(signal_id: int, action: str) -> str:
        """Write signal feedback: like, dislike, skip, queue, or hide_source."""
        return to_json(
            await _call_async(
                "update_signal_feedback",
                signal_runtime.update_signal_feedback,
                signal_id,
                action,
            )
        )

    @contribute_tool
    async def promote_to_library(source_type: str, source_id: int) -> str:
        """Promote one queued signal or X bookmark into a durable summary request."""
        return to_json(
            await _call_async(
                "promote_to_library",
                signal_runtime.promote_to_library,
                source_type,
                source_id,
            )
        )

    @contribute_tool
    async def set_signal_source_active(source_id: int, is_active: bool) -> str:
        """Enable or disable one subscribed signal source for the scoped MCP user."""
        return to_json(
            await _call_async(
                "set_signal_source_active",
                signal_runtime.set_source_active,
                source_id,
                is_active,
            )
        )

    @contribute_tool
    async def vector_health() -> str:
        """Check vector store availability and fallback readiness."""
        return to_json(await _call_async("vector_health", semantic_service.vector_health))

    @contribute_tool
    async def vector_index_stats(scan_limit: int = 5000) -> str:
        """Return index coverage stats between database summaries and the vector store."""
        return to_json(
            await _call_async("vector_index_stats", semantic_service.vector_index_stats, scan_limit)
        )

    @contribute_tool
    async def vector_sync_gap(max_scan: int = 5000, sample_size: int = 20) -> str:
        """Report sync gaps between database summaries and the vector store index."""
        return to_json(
            await _call_async(
                "vector_sync_gap",
                semantic_service.vector_sync_gap,
                max_scan,
                sample_size,
            )
        )

    for contribution in contributions:
        contribution.register(mcp)


class _NullSignalService:
    async def list_sources(self, limit: int = 50) -> dict[str, Any]:
        return {"sources": []}

    async def list_signals(self, limit: int = 20, status: str | None = None) -> dict[str, Any]:
        return {"signals": []}

    async def update_signal_feedback(self, signal_id: int, action: str) -> dict[str, Any]:
        return {"error": "Signal service is not configured"}

    async def promote_to_library(self, source_type: str, source_id: int) -> dict[str, Any]:
        return {"error": "Signal service is not configured"}

    async def set_source_active(self, source_id: int, is_active: bool) -> dict[str, Any]:
        return {"error": "Signal service is not configured"}


class _NullXSearchService:
    async def search(
        self,
        query: str,
        category: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return {"error": "X search service is not configured"}


class _NullArchiveResearchService:
    async def research(self, query: str, max_sources: int = 12) -> dict[str, Any]:
        return {"error": "Archive research service is not configured"}
