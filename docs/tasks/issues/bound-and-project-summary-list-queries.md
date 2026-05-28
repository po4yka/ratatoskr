---
title: Bound and project summary list queries
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

- [ ] #task Bound and project summary list queries #repo/ratatoskr #area/db #status/backlog ⏫

## Objective
Summary read paths over-fetch: list pagination runs 3 queries (2 COUNT + data) and loads full multi-KB JSONB columns for metadata-only views; `async_get_all_for_user` has NO LIMIT and pulls all JSONB into memory; a smart-collection query defaults to a 10,000-row limit with full JSONB (~50MB allocation).

## Context (evidence)
`app/infrastructure/persistence/repositories/summary_repository.py:130-162` (2 separate COUNTs + `select(Summary, Request)` with all columns); `app/infrastructure/persistence/repositories/summary_repository.py:628-639` (`async_get_all_for_user` no LIMIT, full Summary load); `app/infrastructure/persistence/repositories/collection_repository.py:752-769` (default limit 10000 with full JSONB).

## Scope
Combine the two counts into one `count(*) FILTER (WHERE ...)`; add column projection for list views (id, is_read, is_favorited, lang, created_at, updated_at, is_deleted, input_url) instead of loading `json_payload`/`insights_json`; add a sane LIMIT + keyset pagination to `async_get_all_for_user`; lower/stream the smart-collection batch.

## Acceptance criteria
- List pagination is ≤2 queries and does not transfer large JSONB.
- No unbounded SELECT remains.
- Memory profile of smart-collection eval is bounded.

## Epic
Part of [[epic-fix-database-query-performance]].

## References
- Performance audit finding 5A, 5B, 5C, 7A, 7B (2026-05-28).
