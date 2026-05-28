---
title: Add missing indexes for hot queries
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

- [ ] #task Add missing indexes for hot queries #repo/ratatoskr #area/db #status/backlog ⏫

## Objective
Several columns used in WHERE/ORDER BY/JOIN lack indexes, forcing sequential scans at scale — including the sacred `correlation_id` tracing key. Deliver one reviewed Alembic migration adding them all (use `CREATE INDEX CONCURRENTLY IF NOT EXISTS` following migration 0010's pattern).

## Context (evidence)
`app/db/models/core.py:300,392,423,470,656` (`correlation_id` unindexed on Request/RequestProcessingJob/ProgressEvent/CrawlResult/UserInteraction); `app/db/models/core.py:573-586` (no index on `summaries.is_deleted` or `is_favorited`); `app/db/models/digest.py:15-36` (Channel: only UNIQUE on username; `is_active`/`fetch_error_count`/`last_fetched_at` unindexed); `app/db/models/core.py:676-685` (AuditLog: no indexes at all — `ts`, `event`); `app/db/models/core.py:871` (RefreshToken.family_id unindexed); `summary_embeddings.index_status` unindexed (finding 8A).

## Scope
One Alembic migration adding indexes on `correlation_id` (Request, CrawlResult, RequestProcessingJob), `summaries(is_deleted)` and `(is_favorited)` (or composite partials), `audit_logs(ts)` and `(event)`, `refresh_tokens(family_id)`, `summary_embeddings(index_status)`, and the Channel filter columns; use CONCURRENTLY + autocommit_block.

## Acceptance criteria
- Migration applies cleanly up/down.
- `EXPLAIN` shows index usage on correlation-id trace lookups, summary list filters, and audit-log queries.

## Epic
Part of [[epic-fix-database-query-performance]].

## References
- Performance audit finding 3A,3B,3C,3D,3E,8A (2026-05-28).
