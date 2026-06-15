"""Composition root for the summarize graph (ADR-0010).

STUB. Wires already-constructed port implementations into the port-typed node
dependency bundle (:class:`app.di.types.GraphDependencies`). No ``StateGraph``
is assembled yet -- the graph skeleton and its compile entrypoint land in T5.

``langgraph`` MUST NOT be imported here: the banned-api guard still forbids it
until T1 lifts it (ADR-0001), and even afterwards this seam stays
framework-light. This module exists so sibling tracks have a stable
composition seam to extend, and so the import-linter CI job (which imports
``app.*`` at graph-build) sees no optional-dependency import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.di.types import GraphDependencies

if TYPE_CHECKING:
    from app.application.ports.extraction import ExtractionPort
    from app.application.ports.llm_client import LLMClientProtocol
    from app.application.ports.requests import RequestRepositoryPort
    from app.application.ports.retrieval import RetrievalPort
    from app.application.ports.stream_sink import StreamSinkPort
    from app.application.ports.summaries import SummaryRepositoryPort


def build_graph_dependencies(
    *,
    llm_client: LLMClientProtocol,
    retrieval: RetrievalPort,
    extraction: ExtractionPort,
    stream_sink: StreamSinkPort,
    summaries: SummaryRepositoryPort,
    requests: RequestRepositoryPort,
) -> GraphDependencies:
    """Pack port implementations into the node dependency bundle (STUB).

    T5 extends this to build and compile the ``StateGraph`` (injecting the
    checkpointer from T2). Today it only assembles :class:`GraphDependencies`
    from ports that DI has already constructed.
    """
    return GraphDependencies(
        llm_client=llm_client,
        retrieval=retrieval,
        extraction=extraction,
        stream_sink=stream_sink,
        summaries=summaries,
        requests=requests,
    )
