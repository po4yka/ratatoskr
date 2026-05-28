---
title: Fix digest store N+1 queries
status: backlog
area: db
priority: high
owner: unassigned
epic: epic-fix-database-query-performance
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Fix digest store N+1 queries #repo/ratatoskr #area/db #status/backlog ⏫

## Objective
`async_list_fetchable_subscriptions` runs `1 + 2N` queries (≈41 for 20 channels) because it opens a new transaction per subscription to fetch run-state; `async_find_cached_analysis` runs two sequential queries where one JOIN suffices.

## Context (evidence)
`app/infrastructure/persistence/digest_store.py:77-86` (loop calling `async_get_channel_run_state` per subscription); `app/infrastructure/persistence/digest_store.py:612-641` (`async_get_channel_run_state` opens own transaction, 2 SELECTs); `app/infrastructure/persistence/digest_store.py:855-878` (two sequential queries in `async_find_cached_analysis`).

## Scope
Batch run-state loading with a single `WHERE source.external_id IN (...)` query (or a JOIN in the initial subscription load); replace the two cached-analysis queries with one LEFT JOIN of ChannelPost ↔ ChannelPostAnalysis.

## Acceptance criteria
- `async_list_fetchable_subscriptions` issues O(1) queries regardless of subscription count.
- Cached-analysis lookup is a single query.
- Query-count test added.

## Epic
Part of [[epic-fix-database-query-performance]].

## References
- Performance audit finding 2A, 2C (2026-05-28).
