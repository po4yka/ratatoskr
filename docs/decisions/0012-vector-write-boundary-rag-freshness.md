# ADR 0012: Vector-write boundary & read-your-writes RAG freshness

**Date:** 2026-06-15
**Status:** Implemented — the `persist` node writes a read-your-writes Qdrant point synchronously (byte-identical via `app/infrastructure/vector/summary_point.py`); the Taskiq reconciler handles convergence/backfill. Refines the freshness expectation of [ADR-0005](0005-rag-grounding-policy.md).

## Context

RAG grounding (ADR-0005) grounds a new summary with related **prior** summaries. If summary A is created and then article B is summarized moments later, B's grounding should be able to retrieve A. A bulk/convergence sync layer is eventually consistent, so relying on it alone would mean a just-created summary is not retrievable for one poll interval. A freshness contract and a clear vector-write boundary are needed.

## Decision

- **The synchronous fast path owns freshness.** The `persist` graph node writes the summary's Qdrant point synchronously (byte-identical construction via `app/infrastructure/vector/summary_point.py`) **before the request is marked done**, so the new summary is immediately retrievable for RAG grounding of subsequent requests. **Retrieval is a separate read adapter** over the Qdrant client (implementing `ports/retrieval.py` from ADR-0010). Retrieval logic is not part of the write path.
- **Read-your-writes freshness for RAG:** a completed summary MUST be retrievable for grounding subsequent requests **immediately**. Therefore summary creation **synchronously indexes** the new summary into Qdrant on the write path (the existing fast-path embedding — `app/core/embedding_text.py` + a direct upsert) **before the request is marked done**.
- **The Taskiq reconciler (`ratatoskr.vector.reconcile`, cron) is the bulk / backfill / repair layer** that converges the index and heals drift. It is explicitly **not** the RAG freshness guarantee.
- The `user_scope` / `environment` scope filter is mandatory on every retrieval (per ADR-0005).

## Consequences

- The summary write path gains a synchronous Qdrant upsert (small added latency) — the cost of read-your-writes.
- **Write correctness is never blocked on Qdrant:** if the synchronous upsert fails, it is logged and left to the reconciler; the summary still persists, and only the *next* request's grounding may lag once (acceptable degradation).
- Clear ownership: **fast-path = freshness; reconciler = convergence/repair.** No retrieval logic inside the write path.
- This tightens ADR-0005's freshness from best-effort-eventual to read-your-writes. Availability remains best-effort (Qdrant down → ungrounded no-op).

## Alternatives rejected

- **Eventual consistency** — simpler/cheaper, but a just-created summary would not ground the next request (rejected in the interview).
- **Read-your-writes by blocking on a bulk-sync poll** — couples request latency to ETL cadence.
