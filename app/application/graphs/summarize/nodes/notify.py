"""``notify`` node -- terminal user notification / interaction update (ADR-0015/0017)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("notify")
async def notify(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Emit the completion notification / interaction update.

    STUB (T5): no-op. Completion + interaction updates via ``deps.stream_sink``
    (ADR-0017) land in T8; Telegram/SSE consumers stay byte-stable.
    """
    return {}
