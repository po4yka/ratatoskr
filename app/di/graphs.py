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


def build_summary_index_adapter(
    *,
    vector_store: Any,
    embedding_service: Any,
    embedding_repository: Any | None = None,
) -> SummaryIndexPort:
    """Construct the read-your-writes summary indexer (ADR-0012).

    Imported lazily so this module stays importable without the vector stack in
    the import-linter / unit-test envs (matches the langgraph lazy-import seam).
    """
    from app.infrastructure.vector.summary_index_adapter import QdrantSummaryIndexAdapter

    return QdrantSummaryIndexAdapter(
        vector_store=vector_store,
        embedding_service=embedding_service,
        embedding_repository=embedding_repository,
    )


def build_summarize_config(cfg: AppConfig) -> SummarizeConfig:
    """Snapshot the AppConfig fields the summarize nodes need into a primitive bundle.

    Keeps nodes free of ``app.config`` (application-no-outward) while sourcing
    model selection from ``ratatoskr.yaml`` (no code default, rule 11). Model
    routing's long-context model is preferred when routing is enabled, else the
    openrouter long-context model -- mirroring ``pure_summary_service``.

    GAP 1: ``routing_enabled`` is sourced from ``cfg.model_routing.enabled`` so
    nodes never import ``app.config``. The actual resolver (``deps.model_router``)
    is wired in :func:`build_summarize_deps` via ``functools.partial``.
    """
    from app.application.graphs.summarize.deps import SummarizeConfig

    routing = cfg.model_routing
    openrouter = cfg.openrouter
    runtime = cfg.runtime
    attachment = cfg.attachment
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
        routing_enabled=bool(routing.enabled),
        preferred_lang=str(getattr(runtime, "preferred_lang", None) or "auto"),
        article_vision_enabled=bool(getattr(attachment, "article_vision_enabled", False)),
        article_vision_min_images=int(getattr(attachment, "article_vision_min_images", 1)),
        vision_model=getattr(attachment, "vision_model", None),
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
    model_router: Any | None = None,
    summary_cache: Any | None = None,
    crawl_repo: Any | None = None,
    export_events: Any | None = None,
) -> SummarizeDeps:
    """Pack already-constructed ports + config snapshot into the node dependency bundle.

    ``config`` / ``rag_enabled`` / ``rag_top_k`` come from ``AppConfig`` at this
    composition root so the nodes never import ``app.config``
    (application-no-outward).

    GAP 1: ``model_router`` is a ``functools.partial`` of
    ``resolve_model_for_content`` pre-bound with ``routing_config`` and
    ``openrouter_config``; signature ``(tier, content_length) -> str``.

    GAP 2: ``summary_cache`` is a :class:`SummaryCachePort` adapter wrapping
    ``LLMSummaryCache``; wired here so nodes never import adapters.

    GAP 4: ``crawl_repo`` is a :class:`~app.application.ports.requests.CrawlResultRepositoryPort`
    used by the persist node to backfill missing metadata fields from the scrape result.
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
        model_router=model_router,
        summary_cache=summary_cache,
        crawl_repo=crawl_repo,
        export_events=export_events,
    )


def build_summarize_graph_app(*, deps: SummarizeDeps, checkpointer: Any | None = None) -> Any:
    """Compile the summarize graph, defaulting to an in-memory checkpointer.

    When ``checkpointer`` is ``None`` an ``InMemorySaver`` is used (testable without
    the Postgres pool, and the behavior when ``LANGGRAPH_CHECKPOINT_ENABLED`` is
    off). Production wiring passes ``CheckpointerRuntime.saver`` (the
    ``AsyncPostgresSaver``) here via :func:`assemble_graph_url_processor` ->
    :func:`build_url_processor`. langgraph is imported lazily so importing this
    module never requires the ``graph`` extra.
    """
    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        # Non-durable: log so a production wiring that forgot the Postgres saver
        # (CheckpointerRuntime.saver) is observable rather than silent.
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


def build_model_router(cfg: AppConfig) -> Any:
    """Build the content-aware tier router for ``SummarizeDeps.model_router`` (FIX-1).

    A ``lambda (tier, content_length) -> str`` binding ``has_images=False`` and the
    routing/openrouter config. NOT ``functools.partial`` --
    ``resolve_model_for_content`` is keyword-only, so a positional partial would
    ``TypeError``. Returns ``None`` when routing is disabled so the conservative
    path (long-context override / base model) is taken.

    ``has_images=False`` is deliberate (audit #2): article-vision routing is resolved
    UPSTREAM in the ``build_prompt`` node (it owns the image set + multimodal message
    assembly and gives vision priority over content-tier), so by the time this tier
    router is consulted ``model_override`` is already pinned when vision applies. The
    router therefore never needs to re-derive the vision model.
    """
    if not cfg.model_routing.enabled:
        return None
    from app.core.model_router import resolve_model_for_content

    def _route(tier: Any, content_length: int) -> str:
        return resolve_model_for_content(
            tier=tier,
            content_length=content_length,
            has_images=False,
            routing_config=cfg.model_routing,
            openrouter_config=cfg.openrouter,
        )

    return _route


def build_summary_cache_adapter(cfg: AppConfig, *, cache: Any | None = None) -> Any:
    """Build the Redis-backed summary cache port for ``SummarizeDeps`` (FIX-2).

    TTL is sourced from ``cfg.redis.llm_ttl_seconds`` (not hardcoded) and the
    prompt_version from ``cfg.runtime.summary_prompt_version``. The key is also
    namespaced by ``cfg.vector_store.environment`` / ``user_scope`` so dev and
    prod (or two tenant scopes) sharing one Redis never read each other's
    summaries -- see :mod:`app.adapters.content.summary_cache_adapter` for the
    deliberate divergence from the legacy ``LLMSummaryCache`` key scheme.
    """
    from app.adapters.content.summary_cache_adapter import SummaryCacheAdapter

    if cache is None:
        from app.infrastructure.cache.redis_cache import RedisCache

        cache = RedisCache(cfg)
    return SummaryCacheAdapter(
        cache=cache,
        prompt_version=cfg.runtime.summary_prompt_version,
        ttl_seconds=int(getattr(cfg.redis, "llm_ttl_seconds", 7_200)),
        environment=cfg.vector_store.environment,
        user_scope=cfg.vector_store.user_scope,
    )


def build_graph_url_processor(
    *,
    cfg: AppConfig,
    db: Any,
    graph: Any,
    deps: SummarizeDeps,
    cached_summary_responder: Any,
    post_summary_tasks: Any,
    summary_delivery: Any,
    response_formatter: Any,
    request_repo: RequestRepositoryPort,
    message_persistence: Any | None = None,
    hub_factory: Any | None = None,
    content_extractor: Any | None = None,
    summary_repo: Any | None = None,
    audit_func: Any | None = None,
    summarization_runtime: Any | None = None,
) -> Any:
    """Compose the graph-backed URL-flow facade (T9 cutover seam, ADR-0013).

    The graph + deps must already be built (``deps`` carries the FIX-1/2/4
    model_router / summary_cache / crawl_repo wired via :func:`build_summarize_deps`).
    ``hub_factory`` defaults to the process-wide StreamHub so interactive runs get a
    live stream sink.

    ``message_persistence`` is the persistence facade the facade uses to create the
    request row AND write the ``telegram_messages`` snapshot + User/Chat upserts
    (persist-everything; the request row's owner ``user_id`` backs the IDOR filter).
    It defaults to a fresh ``MessagePersistence(db)`` -- the same collaborator the
    legacy ``PlatformRequestLifecycle`` used -- so re-pointing callers stays a DI swap.

    ``content_extractor`` / ``summary_repo`` / ``audit_func`` are the reach-through
    collaborators exposed on the facade so the extraction/persistence consumers
    (forward, aggregation, MCP, url_handler, api background extract) keep working when
    DI returns the facade -- they are NOT a summarize path (``handle_url_flow`` /
    ``summarize`` always drive the graph). ``summarization_runtime`` is retained only
    so the bot's shutdown drain (``aclose``) reaches the shared follow-up runtime.
    """
    from app.adapters.content.graph_url_processor import GraphURLProcessor

    if message_persistence is None:
        from app.infrastructure.persistence.message_persistence import MessagePersistence

        message_persistence = MessagePersistence(db)

    def _stream_sink_factory() -> StreamSinkPort:
        if hub_factory is not None:
            return build_stream_sink(hub=hub_factory())
        from app.adapters.content.streaming import get_stream_hub

        return build_stream_sink(hub=get_stream_hub())

    return GraphURLProcessor(
        cfg=cfg,
        db=db,
        graph=graph,
        deps=deps,
        stream_sink_factory=_stream_sink_factory,
        streamed_runner=run_summarize_graph_streamed,
        cached_summary_responder=cached_summary_responder,
        post_summary_tasks=post_summary_tasks,
        summary_delivery=summary_delivery,
        response_formatter=response_formatter,
        request_repo=request_repo,
        message_persistence=message_persistence,
        content_extractor=content_extractor,
        summary_repo=summary_repo,
        audit_func=audit_func,
        summarization_runtime=summarization_runtime,
    )


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
        # URL-flow (interactive streamed) runner: two-pass enrich is eligible
        # here, still AND-gated by config.two_pass_enabled (audit #20).
        two_pass_eligible=True,
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


def assemble_graph_url_processor(
    *,
    cfg: AppConfig,
    db: Any,
    content_extractor: Any,
    cached_summary_responder: Any,
    summary_delivery: Any,
    post_summary_tasks: Any,
    response_formatter: Any,
    audit_func: Any,
    summarization_runtime: Any,
    llm_client: LLMClientProtocol,
    request_repo: RequestRepositoryPort,
    summary_repo: SummaryRepositoryPort,
    crawl_result_repo: Any,
    llm_repo: LLMRepositoryPort,
    vector_store: Any | None = None,
    embedding_service: Any | None = None,
    redis_cache: Any | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Assemble the full summarize graph + the graph-backed URL-flow facade (T9 cutover).

    This is the single production seam that makes the graph the ONLY summarize path.
    The facade's reach-through collaborators (``content_extractor`` -- wrapped by the
    extraction port -- ``cached_summary_responder``, ``post_summary_tasks``,
    ``summary_delivery``, ``response_formatter``, ``audit_func``) are constructed by
    :func:`app.di.shared.build_url_processor` and passed straight through; the
    returned facade drives the graph for every summarize. ``summarization_runtime`` is
    retained on the facade only so the bot's shutdown drain (``aclose``) reaches the
    shared follow-up runtime; it is NOT a summarize path.

    ``vector_store`` / ``embedding_service`` back the unified retrieval port (ground
    node) and the read-your-writes summary index (persist node, ADR-0012); both
    tolerate ``None`` (ground is RAG-gated off by default; the index write is
    best-effort and the reconciler backfills).

    ``checkpointer`` is the LangGraph saver the compiled graph persists node state
    into (audit #15). Production passes ``CheckpointerRuntime.saver`` (the Postgres
    ``AsyncPostgresSaver``) when ``LANGGRAPH_CHECKPOINT_ENABLED`` is set; ``None``
    falls back to an ``InMemorySaver`` (the flag-off behavior), so the graph is
    never compiled without a checkpointer.
    """
    from app.di.extraction import build_extraction_port
    from app.adapters.export.dispatcher import SummaryExportDispatcher
    from app.infrastructure.persistence.repositories.embedding_repository import (
        EmbeddingRepositoryAdapter,
    )

    extraction = build_extraction_port(
        content_extractor=content_extractor,
        request_repo=request_repo,
    )
    summary_index = build_summary_index_adapter(
        vector_store=vector_store,
        embedding_service=embedding_service,
        embedding_repository=EmbeddingRepositoryAdapter(db),
    )
    retrieval = _build_retrieval_port_or_stub(
        vector_store=vector_store,
        embedding_service=embedding_service,
        db=db,
    )
    deps = build_summarize_deps(
        llm_client=llm_client,
        retrieval=retrieval,
        extraction=extraction,
        stream_sink=build_stream_sink(),
        summaries=summary_repo,
        requests=request_repo,
        summary_index=summary_index,
        llm_repo=llm_repo,
        config=build_summarize_config(cfg),
        rag_enabled=bool(getattr(cfg.runtime, "summarize_rag_enabled", False)),
        rag_top_k=int(getattr(cfg.runtime, "rag_top_k", 5)),
        model_router=build_model_router(cfg),
        summary_cache=build_summary_cache_adapter(cfg, cache=redis_cache),
        crawl_repo=crawl_result_repo,
        export_events=SummaryExportDispatcher(db),
    )
    graph = build_summarize_graph_app(deps=deps, checkpointer=checkpointer)
    return build_graph_url_processor(
        cfg=cfg,
        db=db,
        graph=graph,
        deps=deps,
        cached_summary_responder=cached_summary_responder,
        post_summary_tasks=post_summary_tasks,
        summary_delivery=summary_delivery,
        response_formatter=response_formatter,
        request_repo=request_repo,
        content_extractor=content_extractor,
        summary_repo=summary_repo,
        audit_func=audit_func,
        summarization_runtime=summarization_runtime,
    )


def _build_retrieval_port_or_stub(
    *, vector_store: Any | None, embedding_service: Any | None, db: Any
) -> RetrievalPort:
    """Build the unified retrieval port, falling back to a no-op when vectors are absent.

    The ``ground`` node only queries retrieval when RAG is enabled (off by default),
    so a missing vector store must not abort construction. When the store/embedding
    pair is present we wire the real Qdrant adapter; otherwise an empty-result stub
    keeps the port contract satisfied.
    """
    if vector_store is not None and embedding_service is not None:
        from app.di.retrieval import build_retrieval_adapter

        return build_retrieval_adapter(
            vector_store=vector_store,
            embedding_service=embedding_service,
            db=db,
        )
    return _NullRetrievalPort()


class _NullRetrievalPort:
    """No-op :class:`RetrievalPort`: returns no hits (used when vectors are unavailable).

    The ``ground`` node only calls ``retrieve`` when RAG is enabled; this stub keeps
    the port contract satisfiable so graph construction never depends on a live
    vector store. Signature matches the keyword-only ``RetrievalPort.retrieve``.
    """

    async def retrieve(self, **_kwargs: Any) -> Any:
        from app.application.dto.vector_search import RetrievalResult

        return RetrievalResult(hits=[], total=0)

    async def find_similar(self, **_kwargs: Any) -> Any:
        from app.application.dto.vector_search import RetrievalResult

        return RetrievalResult(hits=[], total=0)
