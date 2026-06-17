"""``notify`` node -- terminal user notification / interaction update (ADR-0015/0017)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("notify")
async def notify(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Spine terminus -- intentional clean no-op.

    This node is the last node before ``END`` in the summarize graph spine and
    MUST remain here with an empty body for two load-bearing reasons:

    1. **DONE stage trigger**: :class:`~app.adapters.content.streaming.graph_event_bridge.GraphEventBridge`
       maps ``node="notify"`` -> :attr:`~app.application.dto.stream_enums.ProcessingStage.DONE`
       (``_NODE_STAGE`` dict).  The bridge emits ``DONE`` on the ``on_chain_start``
       event for this node, which terminates the per-request ``StreamHub`` stream.
       Removing this node breaks the streamed DONE signal for all SSE/Telegram
       progress consumers.

    2. **Transport-concern seam**: terminal done/error notifications to Telegram or
       SSE consumers are dispatched by
       ``BackgroundProgressPublisher`` (wired to ``deps.stream_sink``), NOT from
       this node body.  Keeping the body empty preserves that seam and avoids
       coupling the graph spine to transport concerns (ADR-0017).

    Do NOT add side-effects here.  Do NOT remove this node from the spine.
    """
    return {}
