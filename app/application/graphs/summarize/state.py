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

from typing import Any, TypedDict

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

    # Working fields populated as the graph advances.
    grounding_ids: list[str]
    summary: dict[str, Any]
    validation_errors: list[str]
    repair_attempts: int
    call_count: int
