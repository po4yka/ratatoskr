# ADR 0017: Streaming under the graph

**Date:** 2026-06-15
**Status:** Implemented — streaming is bridged through the `stream_sink` port (`app/application/ports/stream_sink.py`) and `app/adapters/content/streaming/graph_event_bridge.py`, which maps graph node transitions / token deltas to SSE + Telegram drafts without polluting checkpoint state.

## Context

The current pipeline streams partial summary output via the in-process StreamHub pub/sub (`app/adapters/content/streaming/`), which feeds both SSE and Telegram drafts. The graph rewrite (ADR-0015) must preserve streaming without breaking checkpointing (ADR-0004/0011).

## Decision

- **Produce stream events with LangGraph `astream_events`** (token / node events) from the `summarize` node; an adapter bridges these into the existing **StreamHub**. StreamHub remains the pub/sub surface (SSE + Telegram-draft consumers unchanged); the graph is the producer.
- **Streaming is a side-channel, not state.** Streamed tokens are **not** part of checkpointed graph state (state holds only finalized node outputs, per ADR-0011). On resume, the node re-runs and re-streams (or emits the persisted final) — a half-stream is never replayed from a checkpoint.
- The bridge sits behind a **`stream_sink` port** (ADR-0010/0014), so the node stays framework-agnostic.

## Consequences

- Streaming UX is preserved; checkpointing is unaffected (the stream is ephemeral).
- A single bridge (`astream_events` → StreamHub) is the only streaming-coupled surface; SSE and Telegram-draft consumers need no change.

## Alternatives rejected

- **Put stream buffers in graph state** — bloats checkpoints and would replay partial output on resume.
- **Drop streaming during the rewrite** — a visible UX regression (live drafts are a core behavior).
