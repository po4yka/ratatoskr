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
    from collections.abc import Callable

    from app.application.ports.extraction import ExtractionPort
    from app.application.ports.export_events import SummaryExportEventPublisherPort
    from app.application.ports.llm_client import LLMClientProtocol
    from app.application.ports.requests import (
        CrawlResultRepositoryPort,
        LLMRepositoryPort,
        RequestRepositoryPort,
    )
    from app.application.ports.retrieval import RetrievalPort
    from app.application.ports.stream_sink import StreamSinkPort
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.ports.summary_cache import SummaryCachePort
    from app.application.ports.summary_index import SummaryIndexPort
    from app.core.content_classifier import ContentTier


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
    llm_provider: str
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
    # GAP 1: content-aware tier routing. When True, build_prompt calls
    # deps.model_router to pick a per-tier model after long-context routing.
    # Sourced from cfg.model_routing.enabled at the composition root (rule 11).
    routing_enabled: bool = False
    # Output-language preference sourced from ``cfg.runtime.preferred_lang`` at the
    # composition root (default ``auto`` in ``ratatoskr.yaml``). The extract node
    # resolves the final ``state['lang']`` via ``choose_language(preferred_lang,
    # detected_lang)`` so the content's detected language wins under ``auto`` --
    # non-English content is summarized/cached/persisted in its own language rather
    # than the pre-extraction default. A forced ``en``/``ru`` here pins the output.
    preferred_lang: str = "auto"
    # Article-vision routing (audit #2), sourced from ``cfg.attachment`` at the
    # composition root. When ``article_vision_enabled`` is True and the extracted
    # valid-image count reaches ``article_vision_min_images``, build_prompt assembles
    # a multimodal user message and routes to ``vision_model``. Defaults keep the
    # text-only path for bare-mock deps (vision off, no vision model).
    article_vision_enabled: bool = False
    article_vision_min_images: int = 1
    vision_model: str | None = None
    # Maximum characters of source content forwarded to the enrichment LLM pass.
    # Sourced from cfg.runtime.enrichment_content_max_chars at the composition
    # root; default matches the previous hardcoded value (30 000) so existing
    # deployments without the env var are unaffected.
    enrichment_content_max_chars: int = 30000


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
    # Maximum characters of source_text forwarded to the embedding query in the
    # ground node. Bounding this caps both state size and per-call embedding cost.
    # Sourced from RAG_QUERY_MAX_CHARS at the composition root; default 8000 chars
    # (~2 k tokens) is sufficient for semantic similarity without sending full docs.
    rag_query_max_chars: int = 8000
    # Config snapshot for build_prompt/summarize/enrich. Optional so a bare-mock
    # deps (unit tests) yields the conservative path (dynamic budget, base model,
    # no two-pass); production always supplies it.
    config: SummarizeConfig | None = None
    # GAP 1: content-aware tier routing. Injected from the composition root as a
    # lambda that binds has_images=False and keyword args:
    #   lambda tier, n: resolve_model_for_content(
    #       tier=tier, content_length=n, has_images=False,
    #       routing_config=cfg.model_routing, openrouter_config=cfg.openrouter,
    #   )
    # resolve_model_for_content is fully keyword-only; a positional partial would
    # TypeError. Optional -- None means tier routing is disabled (conservative path:
    # use long-context override or the llm_client's configured base model).
    # Signature: (tier: ContentTier, content_length: int) -> str | None
    model_router: Callable[[ContentTier, int], str | None] | None = None
    # GAP 2: Redis LLM summary cache port. Optional -- None means caching is
    # disabled (the node skips lookup + write). Wired at the composition root.
    summary_cache: SummaryCachePort | None = None
    # GAP 4: crawl-result port for metadata backfill in persist. Optional --
    # None means metadata backfill is skipped (conservative path).
    crawl_repo: CrawlResultRepositoryPort | None = None
    export_events: SummaryExportEventPublisherPort | None = None
