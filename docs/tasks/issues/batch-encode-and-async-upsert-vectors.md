---
title: Batch encode and async upsert vectors
status: backlog
area: observability
priority: medium
owner: unassigned
epic: epic-optimize-vector-and-embedding-pipeline
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Batch encode and async upsert vectors #repo/ratatoskr #area/observability #status/backlog 🔼

## Objective

The reconcile loop embeds summaries one-by-one (`model.encode` per row) instead of batch-encoding (5–10× slower on MiniLM); single-row Qdrant upserts use `wait=True` (blocks on disk flush on the hot path); and there is no upsert batching/chunking.

## Context (evidence)

`app/tasks/reconcile_vector_index.py:84-103` (per-row `generate_embedding_for_summary`); `app/infrastructure/vector/qdrant_store.py:306-315` (`wait=True` on every upsert); `:281-315` (`upsert_notes` no internal chunking; callers pass single-element lists).

## Scope

Batch-encode embeddings in the reconcile loop via `generate_embeddings_batch`/`model.encode(list)`; use `wait=False` for non-critical/batch upserts (with a health-check flush) while keeping at-least-once semantics; chunk large upserts into bounded batches.

## Acceptance criteria

- Reconcile uses batch encoding.
- Hot-path summarization upsert latency drops.
- Large upserts are chunked.
- Throughput measured before/after.

## Epic

Part of [[epic-optimize-vector-and-embedding-pipeline]].

## References

- Performance audit findings E-4, V-3, V-4 (2026-05-28).
