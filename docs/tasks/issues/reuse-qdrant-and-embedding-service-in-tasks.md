---
title: Reuse Qdrant and embedding service in tasks
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

- [ ] #task Reuse Qdrant and embedding service in tasks #repo/ratatoskr #area/observability #status/backlog 🔼

## Objective

Several task paths construct a fresh `QdrantClient` (TCP handshake + `get_collections()`) and a fresh `EmbeddingService` (empty model cache) per invocation/repo, multiplying connection probes and model loads (2–10s/slot cold on Pi).

## Context (evidence)

`app/tasks/github_sync.py:591-626` (`_build_analyze_use_case` builds new Qdrant store + `create_embedding_service` per analyzed repo, inside the concurrent `_one` loop); `app/tasks/reconcile_vector_index.py:165-167` (embedding generator rebuilt every run); `app/di/tasks.py:295-311` (`build_x_wiki_sync_task_runtime` new Qdrant + embedding svc per run).

## Scope

Build the Qdrant client and embedding service once per task run (or process-cached, like `get_app_config`/`get_db` lru_cache) and reuse across repos/items; ensure the in-process model cache survives.

## Acceptance criteria

- One Qdrant client + one embedding service per task run (or cached).
- Model loaded once.
- No per-repo handshake.
- Verified by counting client constructions in a test.

## Epic

Part of [[epic-optimize-vector-and-embedding-pipeline]].

## References

- Performance audit findings V-2, T-1, T-2, T-4 (2026-05-28).
