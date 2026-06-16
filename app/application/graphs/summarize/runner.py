"""Flag-gated summarize-graph entrypoint (ADR-0013/0018).

``maybe_run_summarize_graph`` is the thin seam the legacy call sites use during
the migration: it runs the graph only when ``SUMMARIZE_GRAPH_ENABLED`` is set,
otherwise returns ``None`` so the caller falls back to the legacy
``url_processor`` path. The flag is TRANSITIONAL and is removed at the T9 hard
cutover (no flag outlives its migration, ADR-0018).

langgraph-free: it only reads a config bool and delegates to
:func:`app.application.graphs.summarize.graph.run_summarize_graph`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.graph import run_summarize_graph

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.config import AppConfig


def is_summarize_graph_enabled(cfg: AppConfig) -> bool:
    """Whether summarization should route through the graph (transitional flag)."""
    return bool(cfg.runtime.summarize_graph_enabled)


async def maybe_run_summarize_graph(
    *,
    cfg: AppConfig,
    graph: Any,
    deps: SummarizeDeps,
    correlation_id: str,
    request_id: int,
    lang: str,
) -> dict[str, Any] | None:
    """Run the summarize graph iff the flag is on; else ``None`` (legacy fallback).

    Returns the runner result (final state, or ``{"error": ...}`` on terminal
    failure). ``None`` means the flag is off and the caller must use the legacy
    path -- this is the strangler-fig seam, not a failure.
    """
    if not is_summarize_graph_enabled(cfg):
        return None
    return await run_summarize_graph(
        graph=graph,
        deps=deps,
        correlation_id=correlation_id,
        request_id=request_id,
        lang=lang,
    )
