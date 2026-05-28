---
title: "Epic: Optimize vector store and embedding pipeline"
kind: epic
status: backlog
area: observability
priority: high
owner: unassigned
blocks: []
blocked_by: []
children:
  - fix-qdrant-scroll-pagination-truncation
  - cache-embeddings-in-cocoindex-bridge
  - use-true-gemini-batch-embedding-api
  - reuse-qdrant-and-embedding-service-in-tasks
  - batch-encode-and-async-upsert-vectors
  - pipeline-reconciliation-inspect-queries
  - trim-embedding-load-and-cocoindex-parse
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Epic: Optimize vector store and embedding pipeline #repo/ratatoskr #area/observability #status/backlog #epic ⏫

## Objective

The vector subsystem has correct fundamentals (deterministic point IDs, payload indexes, `with_vectors=False` on reconciliation scrolls). But the audit found a silent correctness failure — every Qdrant `scroll()` discards `next_page_offset` and caps at 5000 points, so any deployment with >5k summaries perpetually reports vectors as "missing" and re-embeds them — plus large redundant-compute sources: the CocoIndex embedding bridge bypasses the Redis embedding cache (full re-embed of history on every restart), the Gemini "batch" path is N parallel single calls, and Qdrant clients + embedding services are rebuilt per task run (losing the in-process model cache). This epic makes the embedding/vector pipeline correct and cheap, which matters most on the Raspberry Pi deployment.

## Why this is an epic

The children all touch the embedding → Qdrant write/read pipeline and share one verification surface — the reconciler reports zero false-missing vectors at scale, and a process restart does not trigger a full re-embed. They are independent optimizations unified by the goal of correct, non-redundant vector indexing.

## Child tasks

- [[fix-qdrant-scroll-pagination-truncation]] — V-1: `scroll()` discards `next_page_offset`, 5000-point cap (correctness)
- [[cache-embeddings-in-cocoindex-bridge]] — E-1/CO-1: `embed_text_sync` bypasses the Redis embedding cache; full rescan re-embeds
- [[use-true-gemini-batch-embedding-api]] — E-2: batch embedding is N parallel single calls, no rate limit
- [[reuse-qdrant-and-embedding-service-in-tasks]] — V-2/T-1/T-2/T-4: new QdrantClient + EmbeddingService per task/repo
- [[batch-encode-and-async-upsert-vectors]] — E-4/V-3/V-4: one-by-one encode, `wait=True` flush, no upsert batching
- [[pipeline-reconciliation-inspect-queries]] — V-5: 7 sequential DB queries + scroll, not pipelined
- [[trim-embedding-load-and-cocoindex-parse]] — E-3/S-2/CO-2: probe forward pass on load, double JSON parse, missing entity_type filter

## Definition of done

- All child tasks closed.
- The reconciler correctly enumerates all indexed points regardless of collection size (scroll loops to exhaustion); no false re-embedding at >5k rows.
- A FastAPI restart does not recompute embeddings for already-indexed content (cache hit or watermark skip).
- Gemini embedding uses the true batch endpoint with a concurrency/rate cap; reconcile encodes in batches.

## References

- Performance audit findings V-1..V-5, E-1..E-4, CO-1/CO-2, T-1/T-2/T-4, S-2 (2026-05-28).
- Verified: `app/infrastructure/vector/qdrant_store.py:489,532,575,622` (`records, _ = client.scroll(... limit=limit or 5000)`).
- `app/infrastructure/cocoindex/embedding_bridge.py`, `app/infrastructure/embedding/gemini_embedding_service.py`, `app/tasks/github_sync.py`, `app/tasks/reconcile_vector_index.py`.
