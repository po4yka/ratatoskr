---
title: Use bulk writes for channel posts and reorder
status: backlog
area: db
priority: low
owner: unassigned
epic: epic-fix-database-query-performance
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Use bulk writes for channel posts and reorder #repo/ratatoskr #area/db #status/backlog 🔽

## Objective
Channel posts are inserted one-by-one via `session.add()` and collection item reorder issues one UPDATE per item, where bulk statements would do.

## Context (evidence)
`app/infrastructure/persistence/digest_store.py:479-495` (per-row `session.add(ChannelPost(...))`); `app/infrastructure/persistence/repositories/collection_repository.py:382-392` (N individual UPDATEs in `async_reorder_items`, while `async_reorder_collections:197-208` already uses a CASE bulk UPDATE).

## Scope
Use `insert().values([...])` for channel-post batches; convert `async_reorder_items` to a single CASE-expression bulk UPDATE mirroring `async_reorder_collections`.

## Acceptance criteria
- Post insert issues one bulk statement per batch.
- Reorder issues one UPDATE.
- Behavior is unchanged.
- Tests added.

## Epic
Part of [[epic-fix-database-query-performance]].

## References
- Performance audit finding 6A, 6B (2026-05-28).
