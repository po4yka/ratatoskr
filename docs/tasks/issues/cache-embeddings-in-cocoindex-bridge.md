---
title: Cache embeddings in CocoIndex bridge
status: backlog
area: observability
priority: high
owner: unassigned
epic: epic-optimize-vector-and-embedding-pipeline
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Cache embeddings in CocoIndex bridge #repo/ratatoskr #area/observability #status/backlog ⏫

## Objective

The CocoIndex `embed_text_sync` bridge calls `generate_embedding` directly, bypassing the Redis `EmbeddingCache.get_or_compute` path used by the fast summarization pipeline. On a full rescan (e.g. after a restart, when `FlowLiveUpdater` re-initializes), every historical row is re-embedded from scratch — the single largest redundant-compute source, painful on Pi ARM64 (~200–500ms/embedding).

## Context (evidence)

`app/infrastructure/cocoindex/embedding_bridge.py:45-59` (`embed_text_sync` → `_service.generate_embedding` with no cache); `app/infrastructure/cocoindex/flow.py:248-298` (embed per row); `app/infrastructure/cocoindex/runtime.py:94` (`FlowLiveUpdater.__enter__` re-init on startup).

## Scope

Route `embed_text_sync` through the Redis embedding cache (`get_or_compute`) keyed by content hash + model; ensure the cache is shared with the fast path so a value computed by either is reused.

## Acceptance criteria

- A restart does not recompute embeddings already present in the cache/Qdrant.
- Cache hit-rate measured on a rescan.
- Embedding compute count drops to near-zero for unchanged content.

## Epic

Part of [[epic-optimize-vector-and-embedding-pipeline]].

## References

- Performance audit findings E-1, CO-1 (2026-05-28).
