---
title: Fix audit log filter correctness and pushdown
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

- [ ] #task Fix audit log filter correctness and pushdown #repo/ratatoskr #area/db #status/backlog 🔼

## Objective
`async_audit_log` applies the `user_id` filter in Python AFTER fetching rows, returns an incorrect `total` (computed without the filter then decremented), and paginates by OFFSET on an unindexed `ts`. The JSONB `user_id` lookup should be pushed into SQL.

## Context (evidence)
`app/infrastructure/persistence/repositories/admin_read_repository.py:358-401` (Python-side filter, wrong total, offset pagination); `:388-391` (`details.get("user_id")` in Python instead of `details_json @> '{"user_id": ?}'`).

## Scope
Push the user_id filter into the SQL WHERE with a JSONB containment operator + GIN index on `audit_logs.details_json`; compute `total` with the same filter; switch to keyset pagination (depends on the `ts` index from add-missing-indexes-for-hot-queries).

## Acceptance criteria
- `total` matches the number of rows satisfying all filters.
- The filter is in SQL.
- Pagination uses the index.
- Correctness test added.

## Epic
Part of [[epic-fix-database-query-performance]].

## References
- Performance audit finding 5D, 5F, 7C (2026-05-28).
