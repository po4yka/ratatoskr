---
title: "Epic: Fix caching correctness and metrics cardinality"
kind: epic
status: backlog
area: observability
priority: high
owner: unassigned
blocks: []
blocked_by: []
children:
  - cap-llm-metrics-label-cardinality
  - scope-trending-cache-invalidation-and-fix-race
  - add-stampede-protection-to-embedding-cache
  - clean-up-dead-cache-code-and-token-cache-ttl
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Epic: Fix caching correctness and metrics cardinality #repo/ratatoskr #area/observability #status/backlog #epic ⏫

## Objective

The caching layer is mostly sound (SCAN-based non-blocking clears, double-checked client init, fail-open on errors, binary-packed embedding serialization). But the audit found two correctness/efficiency problems and a metrics-cardinality footgun that threatens the Pi-hosted Prometheus: the `model` label on 14 LLM metrics (and 3-series-per-model circuit-breaker gauge) grows unbounded as OpenRouter routes to new models; `trending_cache._clear_redis()` wipes ALL `ratatoskr:*` keys (auth, embeddings, queries) on every summary write; the trending cache has a TOCTOU race that fetches from the DB outside the lock; and the embedding cache has no stampede protection.

## Why this is an epic

These are independent observability/caching fixes sharing one theme — bounded, correct cache and metric state — and one verification surface (Prometheus series count stays bounded; cache invalidation is scoped and race-free). Grouping keeps the low-severity cleanups from being dropped.

## Child tasks

- [[cap-llm-metrics-label-cardinality]] — M-1/M-2/M-3: `model` label on 14 metrics; 3-series circuit-breaker gauge; free-form `platform`
- [[scope-trending-cache-invalidation-and-fix-race]] — C-2/C-3: `_clear_redis` wipes all keys; DB fetched outside the lock
- [[add-stampede-protection-to-embedding-cache]] — C-1/S-1: no singleflight in `get_or_compute`; double `tolist()` on set
- [[clean-up-dead-cache-code-and-token-cache-ttl]] — C-4/C-5: dead in-memory `QueryCache`; 7-day auth-token cache TTL revocation gap

## Definition of done

- All child tasks closed.
- Prometheus series count is bounded — the `model` label is dropped or bucketed; circuit-breaker state is a single integer gauge per model.
- Summary writes invalidate only the trending prefix, not the whole cache namespace.
- Concurrent misses for the same uncached content trigger a single compute (singleflight).

## References

- Performance audit findings M-1..M-3 (metrics), C-1..C-5, S-1 (2026-05-28).
- `app/observability/metrics.py`, `app/infrastructure/cache/trending_cache.py:187`, `app/infrastructure/cache/embedding_cache.py`, `app/db/query_cache.py`, `app/config/redis.py`.
