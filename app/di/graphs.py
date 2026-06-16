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
from app.application.graphs.summarize.graph import build_summarize_graph

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
