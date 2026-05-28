---
title: Clean up dead cache code and token cache TTL
status: backlog
area: observability
priority: low
owner: unassigned
epic: epic-fix-caching-correctness-and-metrics-cardinality
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Clean up dead cache code and token cache TTL #repo/ratatoskr #area/observability #status/backlog 🔽

## Objective

The in-memory `QueryCache` (lru_cache-backed singleton) has zero call sites — dead code that is a footgun if revived (no cross-worker invalidation). Separately, the auth-token cache TTL of 7 days means a revoked token that was never cached can have no `is_revoked` flag written, lingering in cache for up to a week (auth-correctness edge, low likelihood).

## Context (evidence)

`app/db/query_cache.py:143` (`_default_cache = QueryCache(max_size=128)`, no `@cache_query` call sites); `app/config/redis.py:53-57` (`REDIS_AUTH_TOKEN_CACHE_TTL_SECONDS = 604800`); `AuthTokenCache.mark_revoked` only updates already-cached tokens.

## Scope

Delete the unused in-memory `QueryCache` (or document why it stays); ensure revocation writes a tombstone for tokens not previously cached, or shorten the TTL; verify `invalidate_token` covers the never-cached path.

## Acceptance criteria

- Dead `QueryCache` removed or justified.
- Revoked tokens cannot serve from cache regardless of prior caching.
- Documented.

## Epic

Part of [[epic-fix-caching-correctness-and-metrics-cardinality]].

## References

- Performance audit findings C-4, C-5 (2026-05-28).
