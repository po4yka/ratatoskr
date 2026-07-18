"""``SummarizeState`` -- the serializable, id-based summarize-graph state (ADR-0011).

Invariants (ADR-0011 / ADR-0018):

- **Serializable primitives only.** Every field is ``str`` / ``int`` / ``list`` /
  ``dict`` of primitives so the whole state stays cheap and PII-free. A port,
  session, ``Database``, or live object in state breaks checkpointing at *runtime*,
  not type-check. Note the langgraph serializer can *encode* richer types
  (Pydantic / datetime / dataclass) via msgpack EXT tags WITHOUT raising even under
  ``LANGGRAPH_STRICT_MSGPACK`` (which only governs the pickle fallback for unknown
  types) -- so the real guard against a non-primitive leak is the JSON-primitive
  round-trip test, not msgpack-encodability alone (ADR-0011).
- **Minimal / id-based checkpoints.** Identity and extraction *handles* are ids, not
  bulk content -- ``request_id`` / ``dedupe_hash`` / ``summary_id`` rather than the
  crawl row or persisted summary blob, so most checkpoints stay small and PII-light.
  Seven runtime fields are transient handoffs: ``source_text``, ``grounding_block``,
  ``requested_system_prompt``, ``feedback_instructions``, ``system_prompt``,
  ``messages`` and ``content_for_summary`` carry bulk text. Graph assembly maps
  all seven to LangGraph ``UntrackedValue`` channels, so they remain available to
  adjacent nodes in-process but never enter Postgres checkpoints:

  - ``source_text`` -- written by ``extract``, read by ``ground`` /
    ``build_prompt`` / ``enrich`` / ``persist``. The one bulk *seed* (the
    content-only ``summarize`` entrypoint provides it directly with no request row
    to re-fetch from), so it must live in state.
  - ``grounding_block`` -- the ``ground`` -> ``build_prompt`` seam (ADR-0015);
    empty when RAG is off.
  - ``system_prompt`` / ``messages`` / ``content_for_summary`` -- the
    ``build_prompt`` -> ``summarize`` -> ``repair`` handoff; rebuilding them in
    ``summarize`` from ``source_text`` + ``grounding_block`` would duplicate prompt
    assembly and risk T9 parity drift, so they are passed through state instead.

  On durable resume, nodes re-fetch source content through ``request_id`` from the
  crawl/request repositories and deterministically rebuild grounding/prompt context.
  Content-only runs without a request row remain transient by definition. The state
  tests guard both the reviewed bulk set and its untracked channel mapping.
- **Live dependencies are never in state.** Ports/repositories are bound to nodes
  via ``functools.partial`` at graph-build time (see :mod:`deps` / :mod:`graph`),
  never serialized here.
- **``correlation_id`` is sacred.** It is the graph ``thread_id``; no node mutates it.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

# Max validate -> repair -> validate cycles before the repair node declares the
# call budget exhausted (routed to the single terminal-failure path). The
# langgraph ``recursion_limit`` (set per-invocation) is the independent backstop.
MAX_REPAIR_ATTEMPTS = 3


class SummarizeState(TypedDict, total=False):
    """Checkpoint state for the summarize graph (serializable primitives only)."""

    # Identity -- established at ingest; correlation_id == thread_id (sacred).
    # ``request_id`` is ``None`` for the content-only summarize path (the T9 facade
    # ``summarize`` entrypoint has no request row); the persist node short-circuits
    # all DB writes when it is ``None`` rather than INSERTing a Summary against a
    # non-existent ``requests.id`` (FK violation). The URL path always supplies a
    # real id. ``0`` is NEVER a valid sentinel -- it FK-violates exactly like any
    # other absent row.
    correlation_id: str
    request_id: int | None
    lang: str
    # Raw input URL the extract node resolves through the extraction port. Settled
    # by ingest/the runner; a small primitive str (not bulk content).
    input_url: str
    # Mode flag (NOT a stream buffer): the streaming runner sets it True so the
    # summarize node takes the token-streaming LLM path (deltas dispatched as
    # ``summary_token`` custom events, bridged to StreamHub by T8). The ainvoke
    # path leaves it False so T9 parity stays byte-identical. Streamed tokens are
    # NEVER stored in state -- only this bool (ADR-0011/0017).
    stream: bool
    # Whether the optional two-pass ``enrich`` node may run for this invocation
    # (audit #20). Set True ONLY by the URL-flow runners (``run_summarize_graph``
    # / ``run_summarize_graph_streamed``); the content-only ``summarize``
    # entrypoint leaves it False so the enrichment pass stays restricted to the
    # URL path, matching the legacy two-pass scoping. Gated AND-wise with
    # ``config.two_pass_enabled`` (default False), so this is dormant today. A
    # plain serializable bool, never a buffer.
    two_pass_eligible: bool

    # Extraction handles written by the extract node (id-based; bulk content lives
    # in source_text / the crawl row, not duplicated here). ADR-0015.
    dedupe_hash: str
    content_source: str
    detected_lang: str
    title: str
    # Article-image URLs the extract node lifts from the extraction result (audit
    # #2). A serializable list[str] of HTTPS URLs (not bulk content); build_prompt
    # routes image-rich articles to the vision model + a multimodal user message
    # when the valid count reaches ``article_vision_min_images``.
    images: list[str]

    # Retrieval scope for the ground node's mandatory IDOR-safe filter
    # (ADR-0005/0012/0016). Populated by ingest/extract (T7) from config + the
    # request owner; the ground node no-ops if any is absent.
    user_scope: str
    environment: str
    user_id: int

    # ground's RAG query: the extracted source text the ground node embeds to
    # find related prior summaries. A serializable str (not a live object), but
    # the only bulk field here -- ground is the one node that needs the content
    # and may only import the retrieval port, so it is handed the text via state
    # (ponytail: a content handle could replace it once extract owns re-fetch).
    source_text: str
    # Trusted application-level prompt overrides supplied by PureSummaryRequest.
    requested_system_prompt: str
    feedback_instructions: str

    # Working fields populated as the graph advances.
    grounding_ids: list[str]
    # Anti-contamination "related prior summaries (reference only)" block written
    # by ground (empty when RAG is off / no hits) and concatenated into the
    # system prompt by build_prompt -- the ground<->build_prompt seam (ADR-0015).
    grounding_block: str
    # Assembled system prompt (build_prompt seam; full base-prompt assembly is
    # T7). build_prompt appends grounding_block here.
    system_prompt: str
    # build_prompt -> summarize handoff (all serializable primitives): the full
    # LLM messages list, the cleaned content the summarize node re-uses for
    # injection metadata + two-pass enrichment, the optional model override, and
    # the dynamic output-token budget.
    messages: list[dict[str, Any]]
    content_for_summary: str
    model_override: str
    max_tokens: int
    summary: dict[str, Any]
    # DB id of the persisted summary; set when the summary row exists so the
    # persist node's read-your-writes fast-path can build the Qdrant point id.
    summary_id: int
    validation_errors: list[str]
    repair_attempts: int
    call_count: int
    # Accumulated serializable llm-call records (one per summarize/repair LLM
    # call) the persist node writes to ``llm_calls`` with
    # ``attempt_trigger='graph_node'`` (persist-everything). ``operator.add`` is a
    # stdlib reducer so concurrent/sequential node updates append rather than
    # overwrite -- no LangGraph type leaks into application state (ADR-0018).
    llm_calls: Annotated[list[dict[str, Any]], operator.add]
