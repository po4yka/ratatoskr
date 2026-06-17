# ADR 0012: CocoIndex role boundary & read-your-writes RAG freshness

> **Update (2026-06):** the CocoIndex live updater described here was removed; the read-your-writes fast path + the Taskiq reconciler remain the two vector writers. See [docs/research/cocoindex-integration.md](../research/cocoindex-integration.md) for the rationale.

**Date:** 2026-06-15
**Status:** Implemented — the `persist` node writes a read-your-writes Qdrant point synchronously (byte-identical via `app/infrastructure/vector/summary_point.py`); CocoIndex + the reconciler are convergence/backfill only. Refines the freshness expectation of [ADR-0005](0005-rag-grounding-policy.md).

## Context

RAG grounding (ADR-0005) grounds a new summary with related **prior** summaries. If summary A is created and then article B is summarized moments later, B's grounding should be able to retrieve A. CocoIndex's live ETL is poll-based (eventually consistent), so relying on it alone would mean a just-created summary is not retrievable for one poll interval. A freshness contract and a clear CocoIndex boundary are needed.

## Decision

- **CocoIndex stays write-side ETL only** (DB → Qdrant bulk/incremental sync). **Retrieval is a separate read adapter** over the Qdrant client (implementing `ports/retrieval.py` from ADR-0010). Retrieval logic is **not** built into CocoIndex flows.
- **Read-your-writes freshness for RAG:** a completed summary MUST be retrievable for grounding subsequent requests **immediately**. Therefore summary creation **synchronously indexes** the new summary into Qdrant on the write path (the existing fast-path embedding — `app/core/embedding_text.py` + a direct upsert) **before the request is marked done** — it does not wait for CocoIndex's poll cycle.
- **CocoIndex (live) and VectorReconcile (cron) are the bulk / backfill / repair layer** that converges the index and heals drift. They are explicitly **not** the RAG freshness guarantee.
- The `user_scope` / `environment` scope filter is mandatory on every retrieval (per ADR-0005).

## Consequences

- The summary write path gains a synchronous Qdrant upsert (small added latency) — the cost of read-your-writes.
- **Write correctness is never blocked on Qdrant:** if the synchronous upsert fails, it is logged and left to the reconciler; the summary still persists, and only the *next* request's grounding may lag once (acceptable degradation).
- Clear ownership: **fast-path = freshness; CocoIndex/reconciler = convergence/repair.** No retrieval logic inside CocoIndex.
- This tightens ADR-0005's freshness from best-effort-eventual to read-your-writes. Availability remains best-effort (Qdrant down → ungrounded no-op).

## Alternatives rejected

- **Eventual consistency** — simpler/cheaper, but a just-created summary would not ground the next request (rejected in the interview).
- **Read-your-writes by blocking on the CocoIndex poll** — couples request latency to ETL cadence.
