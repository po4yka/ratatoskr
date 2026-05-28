---
title: Batch admin diagnostic count queries
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

- [ ] #task Batch admin diagnostic count queries #repo/ratatoskr #area/db #status/backlog 🔼

## Objective
Admin diagnostics hold one DB connection across many sequential COUNTs, occupying a pool slot for the whole request: `_storage_activity` 12 COUNTs, `async_diagnostics_snapshot` ~35 serial queries, `async_job_status` 6 COUNTs, `async_content_health` 4 COUNTs.

## Context (evidence)
`app/infrastructure/persistence/repositories/admin_read_repository.py:961-992` (12 sequential COUNTs in one session); `:404-427` (~35 serial queries); `:133-172` (6 COUNT scalars); `:174-218` (4 sequential COUNTs).

## Scope
Replace multi-COUNT blocks with single SQL using `count(*) FILTER (WHERE ...)` / window aggregates; where sub-methods are independent, run with `asyncio.gather` each with a short-lived session.

## Acceptance criteria
- Each diagnostic endpoint issues far fewer round-trips and holds a connection briefly.
- Results are unchanged.
- Verified by query-count assertions.

## Epic
Part of [[epic-fix-database-query-performance]].

## References
- Performance audit finding 4A, 4B, 4C, 5E (2026-05-28).
