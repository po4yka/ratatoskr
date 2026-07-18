# ADR 0011: Graph runtime contract — state, serialization, failure→lifecycle, observability

**Date:** 2026-06-15
**Status:** Implemented — `SummarizeState` (`state.py`) holds serializable primitives only, `thread_id == correlation_id` and `recursion_limit` are set per-invocation (`graph.py::run_summarize_graph`), and every terminal failure routes to the single lifecycle sink (`lifecycle.py::route_terminal_failure`).

## Context

Graph state is checkpointed to Postgres (ADR-0004, msgpack-serialized). State must therefore be serializable; failures must reuse the existing request lifecycle; and the non-negotiable observability rules (persist-everything, correlation IDs are sacred) must hold inside the graph just as in the legacy path.

## Decision

### State & serialization

- `SummarizeState` is the in-process `TypedDict` contract and contains serializable primitives only. Required fields are `correlation_id`, `request_id`, and `lang`; working fields include `grounding_ids`, `summary`, `validation_errors`, `repair_attempts`, and `call_count`.
- **Durable checkpoints are ID-only.** Graph assembly maps the seven bulk runtime handoffs (`source_text`, requested prompt/feedback, `grounding_block`, assembled `system_prompt`, `messages`, and `content_for_summary`) to LangGraph `UntrackedValue` channels. Nodes can pass them to adjacent nodes during one process lifetime, but the Postgres saver never writes them.
- On durable URL-run resume, nodes use `request_id` to re-fetch source/crawl text and deterministically rebuild grounding and prompt context. Content-only runs without a request row are transient and do not claim durable resume.
- **Live dependencies are never stored in state.** The LLM port, retrieval port, repositories, and sessions are passed via graph `config` / `functools.partial` at compile time — they are not serializable and would break checkpointing.
- `thread_id = correlation_id` (sacred), so resumable runs preserve the correlation ID.

### Persistence & observability (persist-everything holds inside the graph)

- Every node LLM call persists to `llm_calls` with `attempt_index` + **`attempt_trigger="graph_node"`** — a new value added to the `llm_attempt_trigger` Postgres enum via an Alembic migration.
- Each node runs inside an OpenTelemetry span with `ratatoskr.*` attributes; existing metrics continue.

### Failure semantics

- A node exception, `GraphRecursionError` (recursion limit hit), and call-budget exhaustion **all map to the existing terminal-failure path**: `RequestProcessingJob` → ERROR, user-facing `Error ID: <correlation_id>`, and the existing notification + interaction-update contract. The graph introduces **no parallel error path**.
- `recursion_limit` is set per-invocation in `config` (not at `compile`); the repair loop is bounded by `repair_attempts` in state plus the recursion limit.

## Consequences

- Resume re-reads source content instead of carrying it in the checkpoint; checkpoint rows stay small and exclude prompts/messages.
- An Alembic migration adds `graph_node` to `llm_attempt_trigger` (Postgres enum `ALTER`).
- Retry/graph-call analytics stay uniform with the existing `attempt_index` / `attempt_trigger` model.

## Alternatives rejected

- **Tracked content + payload channels** — heavier checkpoints and a PII-retention burden. Transient `UntrackedValue` handoffs preserve single-run node composition without weakening the durable-state decision.
- **A graph-specific error/lifecycle** — would duplicate logic and risk dropping the correlation-ID error contract.
