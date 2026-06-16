"""``SummarizeDeps`` -- the port-typed dependency bundle for summarize-graph nodes.

Typed ONLY against application ports (ADR-0010), so the graph stays
adapter-agnostic and ``application-no-outward`` stays green: this module imports
nothing from ``app.adapters.*`` / ``app.infrastructure.*``. Concrete adapters are
wired in at the composition root (:mod:`app.di.graphs`) and bound to each node via
``functools.partial`` -- deps live in graph *config*, never in checkpointed
serializable state (ADR-0011).

This lives in the application layer (not ``app.di.types``) precisely because nodes
must not import the ``app.di`` tier; the DI seam depends on this, not the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.application.ports.extraction import ExtractionPort
    from app.application.ports.llm_client import LLMClientProtocol
    from app.application.ports.requests import RequestRepositoryPort
    from app.application.ports.retrieval import RetrievalPort
    from app.application.ports.stream_sink import StreamSinkPort
    from app.application.ports.summaries import SummaryRepositoryPort


@dataclass(frozen=True, slots=True)
class SummarizeDeps:
    """Immutable bundle of application ports the summarize nodes depend on."""

    llm_client: LLMClientProtocol
    retrieval: RetrievalPort
    extraction: ExtractionPort
    stream_sink: StreamSinkPort
    summaries: SummaryRepositoryPort
    requests: RequestRepositoryPort
