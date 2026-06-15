# ADR 0011: Graph runtime contract — state, serialization, failure→lifecycle, observability

**Date:** 2026-06-15
**Status:** Accepted — implementation pending with [ADR-0001](0001-no-langgraph.md).

## Context

Graph state is checkpointed to Postgres (ADR-0004, msgpack-serialized). State must therefore be serializable; failures must reuse the existing request lifecycle; and the non-negotiable observability rules (persist-everything, correlation IDs are sacred) must hold inside the graph just as in the legacy path.

## Decision

### State & serialization

- `SummarizeState` is a `TypedDict` of **serializable primitives only** (str / int / list / dict). Required fields: `correlation_id`, `request_id`, `lang`; working fields: `grounding_ids`, `summary`, `validation_errors`, `repair_attempts`, `call_count`.
- **Minimal state:** store IDs/handles, not bulk content. Source `content` / crawl text is **re-fetched from Postgres by `request_id`** on entry to the node that needs it — lighter checkpoints and less PII at rest (aligns with the ADR-0004 retention/redaction policy).
- **Live dependencies are never stored in state.** The LLM port, retrieval port, repositories, and sessions are passed via graph `config` / `functools.partial` at compile time — they are not serializable and would break checkpointing.
- `thread_id = correlation_id` (sacred), so resumable runs preserve the correlation ID.

### Persistence & observability (persist-everything holds inside the graph)

- Every node LLM call persists to `llm_calls` with `attempt_index` + **`attempt_trigger="graph_node"`** — a new value added to the `llm_attempt_trigger` Postgres enum via an Alembic migration.
- Each node runs inside an OpenTelemetry span with `ratatoskr.*` attributes; existing metrics continue.

### Failure semantics

- A node exception, `GraphRecursionError` (recursion limit hit), and call-budget exhaustion **all map to the existing terminal-failure path**: `RequestProcessingJob` → ERROR, user-facing `Error ID: <correlation_id>`, and the existing notification + interaction-update contract. The graph introduces **no parallel error path**.
- `recursion_limit` is set per-invocation in `config` (not at `compile`); the repair loop is bounded by `repair_attempts` in state plus the recursion limit.

## Consequences

- Resume re-reads source content (cheap) instead of carrying it in the checkpoint; checkpoint rows stay small.
- An Alembic migration adds `graph_node` to `llm_attempt_trigger` (Postgres enum `ALTER`).
- Retry/graph-call analytics stay uniform with the existing `attempt_index` / `attempt_trigger` model.

## Alternatives rejected

- **Full content + payloads in state** — heavier checkpoints and a PII-retention burden (rejected in the 0011 decision interview in favour of minimal state).
- **A graph-specific error/lifecycle** — would duplicate logic and risk dropping the correlation-ID error contract.
