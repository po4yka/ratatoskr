---
title: Scope trending cache invalidation and fix race
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

- [ ] #task Scope trending cache invalidation and fix race #repo/ratatoskr #area/observability #status/backlog 🔼

## Objective

`trending_cache._clear_redis()` calls `RedisCache.clear()` which wipes ALL `ratatoskr:*` keys (auth tokens, embeddings, query results, batch progress) on every summary write — causing a cold-miss latency spike. Separately, the trending cache has a TOCTOU race: it releases the asyncio lock before the DB fetch, so two concurrent misses both scan up to 1000 rows.

## Context (evidence)

`app/infrastructure/cache/trending_cache.py:183-189` (`_clear_redis` → `redis_cache.clear()` wipes everything; `RedisCache.clear()` matches `{prefix}:*` at `redis_cache.py:114`); `app/infrastructure/cache/trending_cache.py:134-166` (lock released at :143, DB query at :148-153 outside any lock).

## Scope

Replace `clear()` with `redis_cache.clear_prefix("trending")`; hold the lock across the DB fetch + store (or use a singleflight) so only one coroutine computes on a miss.

## Acceptance criteria

- Summary writes invalidate only the trending prefix.
- Concurrent trending misses trigger a single DB scan.
- Tests cover both.

## Epic

Part of [[epic-fix-caching-correctness-and-metrics-cardinality]].

## References

- Performance audit findings C-2, C-3 (2026-05-28).
