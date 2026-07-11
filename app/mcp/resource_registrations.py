"""MCP resource registration adapters — thin wrappers required by the MCP framework.

Each function is a single-line adapter: wrap the service call, serialize the
result to JSON for the wire protocol, and register it through a schema-testable
contribution. No domain logic lives here; all business logic is in the injected
service classes.
"""

from __future__ import annotations

from collections.abc import Callable  # noqa: TC003
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.mcp.helpers import to_json
from app.mcp.tool_registrations import mcp_rate_limit_exceeded

if TYPE_CHECKING:
    from app.mcp.aggregation_service import AggregationMcpService
    from app.mcp.article_service import ArticleReadService
    from app.mcp.catalog_service import CatalogReadService
    from app.mcp.context import McpServerContext
    from app.mcp.semantic_service import SemanticSearchService
    from app.mcp.signal_service import SignalMcpService


class McpResourceRegistrar(Protocol):
    def resource(
        self, uri: str, *args: Any, **kwargs: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Return a FastMCP-compatible resource decorator."""
        ...


class McpResourceContribution(BaseModel):
    """Schema-testable MCP resource contribution."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    uri: str
    handler: Callable[..., Any] = Field(exclude=True)
    name: str
    description: str

    @classmethod
    def from_handler(cls, uri: str, handler: Callable[..., Any]) -> McpResourceContribution:
        return cls(
            uri=uri,
            handler=handler,
            name=handler.__name__,
            description=(handler.__doc__ or "").strip(),
        )

    def register(self, mcp: McpResourceRegistrar) -> None:
        mcp.resource(self.uri)(self.handler)


def _contribute_resource(
    contributions: list[McpResourceContribution],
    uri: str,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        contributions.append(McpResourceContribution.from_handler(uri, fn))
        return fn

    return decorator


def register_resources(
    mcp: Any,
    *,
    context: McpServerContext,
    aggregation_service: AggregationMcpService,
    article_service: ArticleReadService,
    catalog_service: CatalogReadService,
    semantic_service: SemanticSearchService,
    signal_service: SignalMcpService | None = None,
) -> None:
    signal_runtime: Any = signal_service if signal_service is not None else _NullSignalService()
    contributions: list[McpResourceContribution] = []

    def _resource_rate_limited(resource_name: str) -> dict[str, str]:
        return {
            "error": "rate_limited",
            "message": f"MCP resource '{resource_name}' rate limit exceeded; retry later.",
        }

    async def _call_resource(resource_name: str, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Gate a resource's service call through the shared tool-layer rate limiter.

        Mirrors ``register_tools._call_async`` so resources cannot bypass the limiter
        (finding: the 17 resources previously read straight through with no cap). The
        check runs before the service call and returns the same ``rate_limited`` shape
        the tools return; on a hit the underlying DB / vector scan never runs.
        """
        if mcp_rate_limit_exceeded(resource_name, context):
            return _resource_rate_limited(resource_name)
        return await fn(*args, **kwargs)

    @_contribute_resource(contributions, "ratatoskr://aggregations/recent")
    async def recent_aggregations_resource() -> str:
        """Recent aggregation bundles for the scoped MCP user."""
        return to_json(
            await _call_resource(
                "recent_aggregations_resource",
                aggregation_service.list_aggregation_bundles,
                limit=10,
                offset=0,
            )
        )

    @_contribute_resource(contributions, "ratatoskr://aggregations/{session_id}")
    async def aggregation_bundle_resource(session_id: str) -> str:
        """One persisted aggregation bundle for the scoped MCP user."""
        try:
            resolved_session_id = int(session_id)
        except ValueError:
            return to_json({"error": f"Invalid aggregation session ID: {session_id}"})
        return to_json(
            await _call_resource(
                "aggregation_bundle_resource",
                aggregation_service.get_aggregation_bundle,
                resolved_session_id,
            )
        )

    @_contribute_resource(contributions, "ratatoskr://articles/recent")
    async def recent_articles_resource() -> str:
        """A snapshot of the 10 most recent article summaries."""
        return to_json(
            await _call_resource(
                "recent_articles_resource", article_service.list_articles, limit=10, offset=0
            )
        )

    @_contribute_resource(contributions, "ratatoskr://articles/favorites")
    async def favorites_resource() -> str:
        """All favorited article summaries."""
        return to_json(
            await _call_resource(
                "favorites_resource",
                article_service.list_articles,
                limit=50,
                offset=0,
                is_favorited=True,
            )
        )

    @_contribute_resource(contributions, "ratatoskr://articles/unread")
    async def unread_resource() -> str:
        """Unread article summaries (up to 20)."""
        return to_json(
            await _call_resource("unread_resource", article_service.unread_articles, limit=20)
        )

    @_contribute_resource(contributions, "ratatoskr://stats")
    async def stats_resource() -> str:
        """Current database statistics for Ratatoskr."""
        return to_json(await _call_resource("stats_resource", article_service.get_stats))

    @_contribute_resource(contributions, "ratatoskr://tags")
    async def tags_resource() -> str:
        """All topic tags with article counts, sorted by frequency."""
        return to_json(await _call_resource("tags_resource", article_service.tag_counts))

    @_contribute_resource(contributions, "ratatoskr://entities")
    async def entities_resource() -> str:
        """Aggregated entities (people, organizations, locations) across all articles."""
        return to_json(await _call_resource("entities_resource", article_service.entity_counts))

    @_contribute_resource(contributions, "ratatoskr://domains")
    async def domains_resource() -> str:
        """Source domains with article counts, sorted by frequency."""
        return to_json(await _call_resource("domains_resource", article_service.domain_counts))

    @_contribute_resource(contributions, "ratatoskr://collections")
    async def collections_resource() -> str:
        """All top-level collections with item counts."""
        return to_json(
            await _call_resource(
                "collections_resource", catalog_service.list_collections, limit=50, offset=0
            )
        )

    @_contribute_resource(contributions, "ratatoskr://videos/recent")
    async def recent_videos_resource() -> str:
        """10 most recent video downloads with metadata."""
        return to_json(
            await _call_resource(
                "recent_videos_resource",
                catalog_service.list_videos,
                limit=10,
                offset=0,
                status="completed",
            )
        )

    @_contribute_resource(contributions, "ratatoskr://processing/stats")
    async def processing_stats_resource() -> str:
        """Processing statistics: LLM call counts, token usage, model breakdown."""
        return to_json(
            await _call_resource("processing_stats_resource", catalog_service.processing_stats)
        )

    @_contribute_resource(contributions, "ratatoskr://vector/health")
    async def vector_health_resource() -> str:
        """Vector store availability status for semantic MCP tools."""
        return to_json(
            await _call_resource("vector_health_resource", semantic_service.vector_health)
        )

    @_contribute_resource(contributions, "ratatoskr://vector/index-stats")
    async def vector_index_stats_resource() -> str:
        """Vector store index coverage compared to PostgreSQL summaries."""
        return to_json(
            await _call_resource(
                "vector_index_stats_resource", semantic_service.vector_index_stats
            )
        )

    @_contribute_resource(contributions, "ratatoskr://vector/sync-gap")
    async def vector_sync_gap_resource() -> str:
        """Vector store/PostgreSQL sync gap sample using default scan limits."""
        return to_json(
            await _call_resource("vector_sync_gap_resource", semantic_service.vector_sync_gap)
        )

    @_contribute_resource(contributions, "ratatoskr://signals/recent")
    async def recent_signals_resource() -> str:
        """Recent signal candidates for the scoped MCP user."""
        return to_json(
            await _call_resource("recent_signals_resource", signal_runtime.list_signals, limit=20)
        )

    @_contribute_resource(contributions, "ratatoskr://sources")
    async def signal_sources_resource() -> str:
        """Signal source catalog."""
        return to_json(
            await _call_resource("signal_sources_resource", signal_runtime.list_sources, limit=100)
        )

    for contribution in contributions:
        contribution.register(mcp)


class _NullSignalService:
    async def list_sources(self, limit: int = 50) -> dict[str, Any]:
        return {"sources": []}

    async def list_signals(self, limit: int = 20, status: str | None = None) -> dict[str, Any]:
        return {"signals": []}
