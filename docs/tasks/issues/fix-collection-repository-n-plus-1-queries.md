---
title: Fix collection repository N+1 queries
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

- [ ] #task Fix collection repository N+1 queries #repo/ratatoskr #area/db #status/backlog 🔼

## Objective
`_serialize_collection` issues one COUNT query per collection row, so listing N collections fires 1 + N queries.

## Context (evidence)
`app/infrastructure/persistence/repositories/collection_repository.py:68,170,796-803` (`_item_count` called per row in `async_list_collections` and `async_get_collection_tree`).

## Scope
Replace per-row counts with a single grouped `SELECT collection_id, count(*) ... WHERE collection_id = ANY(:ids) GROUP BY collection_id`, then join the map in Python.

## Acceptance criteria
- Listing collections issues O(1) queries regardless of collection count.
- Item counts are unchanged.
- Query-count test added.

## Epic
Part of [[epic-fix-database-query-performance]].

## References
- Performance audit finding 2B (2026-05-28).
