---
title: Tune connection pool and statement cache
status: backlog
area: db
priority: medium
owner: unassigned
epic: epic-fix-database-query-performance
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Tune connection pool and statement cache #repo/ratatoskr #area/db #status/backlog 🔼

## Objective
The async engine sets no explicit `pool_timeout` and no asyncpg statement-cache tuning (risking "cached plan must not change result type" thrash with varying `IN` lists), and a read-only run-state method uses `transaction()` instead of `session()`.

## Context (evidence)
`app/db/session.py:55-71` and `app/config/database.py:23-36` (pool_size 8, max_overflow 4, pool_recycle 900, pre_ping; no pool_timeout; no statement_cache_size); `app/infrastructure/persistence/digest_store.py:612-641` (read-only method uses `transaction()`).

## Scope
Set an explicit `pool_timeout`; pass `connect_args`/`server_settings` to tune or disable the asyncpg prepared-statement cache as appropriate for the parameterized-IN workload; re-evaluate pool sizing for concurrent digest+batch load; switch the read-only method to `session()`.

## Acceptance criteria
- Pool timeout is explicit and documented.
- No prepared-statement cache errors under varying IN-lists.
- Read-only path no longer opens a write transaction.

## Epic
Part of [[epic-fix-database-query-performance]].

## References
- Performance audit finding 1, 4D (2026-05-28).
