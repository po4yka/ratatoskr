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
- **Minimal / id-based.** Store ids and handles, not bulk content. Source text and
  crawl output are re-fetched from Postgres by ``request_id`` inside the node that
  needs them -- lighter checkpoints and less PII at rest.
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
    correlation_id: str
    request_id: int
    lang: str
    # Raw input URL the extract node resolves through the extraction port. Settled
    # by ingest/the runner; a small primitive str (not bulk content).
    input_url: str

    # Extraction handles written by the extract node (id-based; bulk content lives
    # in source_text / the crawl row, not duplicated here). ADR-0015.
    dedupe_hash: str
    content_source: str
    detected_lang: str
    title: str

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
    # overwrite -- no langgraph import (the no-graph-extra invariant, ADR-0018).
    llm_calls: Annotated[list[dict[str, Any]], operator.add]
