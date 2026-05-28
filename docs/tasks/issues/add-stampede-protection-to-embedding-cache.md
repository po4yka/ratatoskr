---
title: Add stampede protection to embedding cache
status: backlog
area: observability
priority: medium
owner: unassigned
epic: epic-fix-caching-correctness-and-metrics-cardinality
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Add stampede protection to embedding cache #repo/ratatoskr #area/observability #status/backlog 🔼

## Objective

`EmbeddingCache.get_or_compute` is a plain cache-aside with no singleflight, so concurrent misses for the same content all call `compute_fn` (N redundant CPU embeds on Pi, or N Gemini calls). Separately, `set` converts the numpy array to a Python list twice.

## Context (evidence)

`app/infrastructure/cache/embedding_cache.py:183-218` (no lock / NX set in `get_or_compute`); `:154-158` (both `serialize_embedding` at :63 and the inline `values =` assignment call `.tolist()`).

## Scope

Add a lightweight singleflight (Redis `SET key marker NX EX` lease or an in-process per-key asyncio lock) so the first writer wins and others wait/skip; compute the list representation once and reuse.

## Acceptance criteria

- Concurrent misses for identical content trigger a single compute.
- Embedding serialized with one numpy→list conversion.
- Tests cover the stampede case.

## Epic

Part of [[epic-fix-caching-correctness-and-metrics-cardinality]].

## References

- Performance audit findings C-1, S-1 (2026-05-28).
