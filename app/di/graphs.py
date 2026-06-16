"""Composition root for the summarize graph (ADR-0010).

Promotes the T3 stub: wires already-constructed application ports into the
port-typed :class:`SummarizeDeps` bundle and compiles the ``StateGraph`` with a
pluggable checkpointer. This module and
``app/application/graphs/summarize/graph.py`` are the ONLY langgraph-coupled
surfaces (ADR-0010/0018); nodes stay framework-free.

langgraph is imported **lazily** inside :func:`build_summarize_graph_app` so this
module stays importable in the import-linter / mypy / unit-test CI envs, which do
not install the optional ``graph`` extra. T5 defaults the checkpointer to an
in-memory saver; T2's ``AsyncPostgresSaver`` is injected at this same seam at
cutover.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.deps import SummarizeDeps
from app.application.graphs.summarize.graph import (
    DEFAULT_RECURSION_LIMIT,
    build_initial_state,
    build_summarize_graph,
    invocation_config,
    reason_code_for_exception,
)
from app.application.graphs.summarize.lifecycle import route_terminal_failure

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeConfig
    from app.application.ports.extraction import ExtractionPort
    from app.application.ports.llm_client import LLMClientProtocol
    from app.application.ports.requests import LLMRepositoryPort, RequestRepositoryPort
    from app.application.ports.retrieval import RetrievalPort
    from app.application.ports.stream_sink import StreamSinkPort
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.ports.summary_index import SummaryIndexPort
    from app.config import AppConfig


def build_summary_index_adapter(*, vector_store: Any, embedding_service: Any) -> SummaryIndexPort:
    """Construct the read-your-writes summary indexer (ADR-0012).

    Imported lazily so this module stays importable without the vector stack in
    the import-linter / unit-test envs (matches the langgraph lazy-import seam).
    """
    from app.infrastructure.vector.summary_index_adapter import QdrantSummaryIndexAdapter

    return QdrantSummaryIndexAdapter(vector_store=vector_store, embedding_service=embedding_service)


def build_summarize_config(cfg: AppConfig) -> SummarizeConfig:
    """Snapshot the AppConfig fields the summarize nodes need into a primitive bundle.

    Keeps nodes free of ``app.config`` (application-no-outward) while sourcing
    model selection from ``ratatoskr.yaml`` (no code default, rule 11). Model
    routing's long-context model is preferred when routing is enabled, else the
    openrouter long-context model -- mirroring ``pure_summary_service``.
    """
    from app.application.graphs.summarize.deps import SummarizeConfig

    routing = cfg.model_routing
    openrouter = cfg.openrouter
    runtime = cfg.runtime
    long_context_model = (
        routing.long_context_model if routing.enabled else openrouter.long_context_model
    )
    threshold = routing.long_context_threshold_tokens if routing.enabled else 80000
    return SummarizeConfig(
        model=openrouter.model,
        temperature=openrouter.temperature,
        structured_output_mode=openrouter.structured_output_mode,
        long_context_threshold_tokens=threshold,
        long_context_model=long_context_model,
        configured_max_tokens=openrouter.max_tokens,
        top_p=openrouter.top_p,
        summarization_max_retries=int(getattr(runtime, "summarization_max_retries", 3)),
        sticky_fallback_enabled=bool(getattr(runtime, "llm_sticky_failure_force_fallback", True)),
        two_pass_enabled=bool(getattr(runtime, "summary_two_pass_enabled", False)),
    )


def build_summarize_deps(
    *,
    llm_client: LLMClientProtocol,
    retrieval: RetrievalPort,
    extraction: ExtractionPort,
    stream_sink: StreamSinkPort,
    summaries: SummaryRepositoryPort,
    requests: RequestRepositoryPort,
    summary_index: SummaryIndexPort,
    llm_repo: LLMRepositoryPort | None = None,
    config: SummarizeConfig | None = None,
    rag_enabled: bool = False,
    rag_top_k: int = 5,
) -> SummarizeDeps:
    """Pack already-constructed ports + config snapshot into the node dependency bundle.

    ``config`` / ``rag_enabled`` / ``rag_top_k`` come from ``AppConfig`` at this
    composition root so the nodes never import ``app.config``
    (application-no-outward).
    """
    return SummarizeDeps(
        llm_client=llm_client,
        retrieval=retrieval,
        extraction=extraction,
        stream_sink=stream_sink,
        summaries=summaries,
        requests=requests,
        summary_index=summary_index,
        llm_repo=llm_repo,
        config=config,
        rag_enabled=rag_enabled,
        rag_top_k=rag_top_k,
    )


def build_summarize_graph_app(*, deps: SummarizeDeps, checkpointer: Any | None = None) -> Any:
    """Compile the summarize graph, defaulting to an in-memory checkpointer.

    When ``checkpointer`` is ``None`` an ``InMemorySaver`` is used (testable without
    the T2 Postgres pool). Production wiring passes T2's ``AsyncPostgresSaver`` here.
    langgraph is imported lazily so importing this module never requires the
    ``graph`` extra.
    """
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        # Non-durable: log so a production wiring that forgot the Postgres saver
        # (T2 AsyncPostgresSaver) is observable rather than silent.
        logger.info("summarize_graph_using_in_memory_checkpointer")
        checkpointer = InMemorySaver()
    return build_summarize_graph(deps=deps, checkpointer=checkpointer)


def build_stream_sink(*, hub: Any | None = None) -> StreamSinkPort:
    """Construct the StreamHub-backed stream sink (ADR-0017).

    Imported lazily so this module stays importable without the streaming stack
    in minimal envs (matches the other adapter seams in this module). Inject the
    result into :func:`build_summarize_deps` as ``stream_sink``.
    """
    from app.adapters.content.streaming.stream_sink_hub import StreamHubStreamSink

    return StreamHubStreamSink(hub=hub)


async def run_summarize_graph_streamed(
    *,
    graph: Any,
    deps: SummarizeDeps,
    sink: StreamSinkPort,
    correlation_id: str,
    request_id: int,
    lang: str,
    input_url: str = "",
    user_scope: str | None = None,
    environment: str | None = None,
    recursion_limit: int | None = None,
) -> dict[str, Any]:
    """Drive the summarize graph via ``astream_events``, bridging events to the sink.

    This is the streaming "runner" (ADR-0017): it owns the bridge lifecycle
    (created on run start, discarded on terminal completion) so the summarize
    node stays framework-agnostic. ``astream_events`` is the single driver -- it
    executes the graph AND yields the events the bridge translates into
    ``StreamHub`` progress; there is no separate ``ainvoke``.

    Streamed tokens are an ephemeral side-channel: the bridge's assembler is
    local to this call and never enters checkpoint state (ADR-0011). Terminal
    ``done`` / ``error`` are emitted by ``BackgroundProgressPublisher``, not here,
    so each request_id stream terminates exactly once.

    On any node/recursion/budget failure, routes to the single terminal-failure
    path and returns ``{"error": ...}``; otherwise returns the final graph state.
    """
    from app.adapters.content.streaming.graph_event_bridge import GraphEventBridge

    limit = DEFAULT_RECURSION_LIMIT if recursion_limit is None else recursion_limit
    # stream=True flips the summarize node onto its token-streaming LLM path so the
    # bridge below actually receives summary_token events (ADR-0017).
    initial_state = build_initial_state(
        correlation_id=correlation_id,
        request_id=request_id,
        lang=lang,
        input_url=input_url,
        user_scope=user_scope,
        environment=environment,
        stream=True,
    )
    config = invocation_config(correlation_id=correlation_id, recursion_limit=limit)
    bridge = GraphEventBridge(sink=sink, request_id=str(request_id), correlation_id=correlation_id)
    final_state: dict[str, Any] = {}
    try:
        async for event in graph.astream_events(initial_state, config=config, version="v2"):
            await bridge.dispatch(event)
            captured = _final_state_from_event(event)
            if captured is not None:
                final_state = captured
        return final_state
    except Exception as exc:
        # Single terminal sink (ADR-0011), mirroring run_summarize_graph: a node
        # exception, GraphRecursionError, or CallBudgetExceeded all route here.
        # BaseException (cancellation) is deliberately not caught.
        logger.warning(
            "summarize_graph_streamed_terminal_failure",
            extra={"correlation_id": correlation_id, "error_type": type(exc).__name__},
        )
        message = await route_terminal_failure(
            initial_state, deps, exc, reason_code=reason_code_for_exception(exc)
        )
        return {"error": message, "correlation_id": correlation_id, "request_id": request_id}


def _final_state_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort: the root graph's ``on_chain_end`` output is the final state.

    The root run has no parents; nested node/runnable ends carry ``parent_ids``.
    Not load-bearing (T9 parity asserts output via ``ainvoke``) -- the streaming
    path returns it as a convenience for callers.
    """
    if event.get("event") != "on_chain_end" or event.get("parent_ids"):
        return None
    output = event.get("data", {}).get("output")
    return output if isinstance(output, dict) else None
