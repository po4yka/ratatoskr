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
    from app.application.ports.requests import LLMRepositoryPort, RequestRepositoryPort
    from app.application.ports.retrieval import RetrievalPort
    from app.application.ports.stream_sink import StreamSinkPort
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.ports.summary_index import SummaryIndexPort


@dataclass(frozen=True, slots=True)
class SummarizeConfig:
    """Primitive snapshot of the AppConfig fields the summarize nodes need.

    Built at the composition root (:mod:`app.di.graphs`) from ``AppConfig`` so
    nodes never import ``app.config`` (application-no-outward). Model-selection
    fields carry NO code default (rule 11 / ADR-0018) -- they are always sourced
    from ``ratatoskr.yaml`` at the composition root. Behavioural toggles default
    to the same values the legacy ``RuntimeConfig`` fields default to.
    """

    model: str
    temperature: float
    structured_output_mode: str | None
    long_context_threshold_tokens: int
    long_context_model: str | None = None
    configured_max_tokens: int | None = None
    top_p: float | None = None
    summarization_max_retries: int = 3
    sticky_fallback_enabled: bool = True
    two_pass_enabled: bool = False
    enrichment_max_tokens: int = 4096


@dataclass(frozen=True, slots=True)
class SummarizeDeps:
    """Immutable bundle of application ports the summarize nodes depend on."""

    llm_client: LLMClientProtocol
    retrieval: RetrievalPort
    extraction: ExtractionPort
    stream_sink: StreamSinkPort
    summaries: SummaryRepositoryPort
    requests: RequestRepositoryPort
    # Read-your-writes fast-path used by the persist node (ADR-0012).
    summary_index: SummaryIndexPort
    # llm_calls writer for the persist node (persist-everything). Optional so a
    # bare-mock deps still constructs; production always supplies it.
    llm_repo: LLMRepositoryPort | None = None
    # RAG grounding knobs injected from RuntimeConfig at the composition root, so
    # nodes never import app.config (application-no-outward). ADR-0018: the flag
    # is transitional and retires at the T6 cutover.
    rag_enabled: bool = False
    rag_top_k: int = 5
    # Config snapshot for build_prompt/summarize/enrich. Optional so a bare-mock
    # deps (unit tests) yields the conservative path (dynamic budget, base model,
    # no two-pass); production always supplies it.
    config: SummarizeConfig | None = None
