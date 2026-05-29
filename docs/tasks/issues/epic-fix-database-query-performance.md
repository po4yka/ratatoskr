---
title: "Epic: Fix database query performance and indexing"
kind: epic
status: backlog
area: db
priority: high
owner: unassigned
blocks: []
blocked_by: []
children:
  - bound-and-project-summary-list-queries
created: 2026-05-28
updated: 2026-05-29
---

- [ ] #task Epic: Fix database query performance and indexing #repo/ratatoskr #area/db #status/doing #epic ⏫

## Status (2026-05-29)

8 of 9 child tasks are **done** (closed): add-missing-indexes-for-hot-queries,
fix-digest-store-n-plus-1-queries, fix-collection-repository-n-plus-1-queries,
batch-admin-diagnostic-count-queries, fix-audit-log-filter-correctness-and-pushdown,
use-bulk-writes-for-channel-posts-and-reorder, tune-connection-pool-and-statement-cache,
reduce-digest-delivery-posts-json-fanout.

One child remains open: [[bound-and-project-summary-list-queries]]. Its safe part
(5A — single-query pagination counts) shipped; the rest (5B/5C/7A/7B — bounding
the full-snapshot sync and smart-collection reads, projecting json_payload out of
list views) was found to be breaking as specified because json_payload is
load-bearing on those paths (titles/tags/content live there). The real fix
(denormalized title/tags columns + streamed sync) needs a migration and touches
the freeze-priority Mobile API contract, so it is tracked in that note as a
follow-up.

## Objective

The data layer has good bones (bulk `INSERT … ON CONFLICT` in the signal repos, partial index on active summaries, `SELECT … FOR UPDATE` on favorite toggle, `pool_pre_ping`). But the audit found N+1 loops in the digest and collection paths, missing indexes on the sacred `correlation_id` tracing key and on common summary/audit filters, unbounded and over-fetching summary queries that pull multi-KB JSONB into Python, long single-connection holds across dozens of sequential COUNTs, and a correctness bug in audit-log pagination. This epic brings query patterns and schema indexing in line with the access patterns.

## Why this is an epic

Every child improves the same subsystem (Postgres access via the `Database` facade) and is verified against the same surface — query counts per request, `EXPLAIN` plans, and connection-occupancy time. They are independent fixes that share one theme (the schema and queries should match the read/write patterns), so grouping prevents them from scattering.

## Child tasks

- [[add-missing-indexes-for-hot-queries]] — 3A..3E/8A: `correlation_id`, `summaries.is_deleted/is_favorited`, `AuditLog`, `Channel`, `RefreshToken.family_id`, `summary_embeddings.index_status`
- [[fix-digest-store-n-plus-1-queries]] — 2A/2C: `1+2N` queries in `async_list_fetchable_subscriptions`; two-query cached-analysis lookup
- [[fix-collection-repository-n-plus-1-queries]] — 2B: one COUNT per collection in `_serialize_collection`
- [[bound-and-project-summary-list-queries]] — 5A/5B/5C/7A/7B: 3 queries per page, unbounded `get_all_for_user`, 10k default limit, full-JSONB loads
- [[batch-admin-diagnostic-count-queries]] — 4A/4B/4C/5E: sessions held across 12–35 sequential COUNTs
- [[fix-audit-log-filter-correctness-and-pushdown]] — 5D/5F/7C: Python-side `user_id` filter, wrong `total`, unindexed offset pagination
- [[use-bulk-writes-for-channel-posts-and-reorder]] — 6A/6B: per-row `session.add()` / per-item UPDATE
- [[tune-connection-pool-and-statement-cache]] — Finding 1/4D: pool sizing, asyncpg statement cache, read uses `transaction()`
- [[reduce-digest-delivery-posts-json-fanout]] — 7D: full `posts_json` blobs loaded and flattened in Python

## Definition of done

- All child tasks closed.
- N+1 loops in digest and collection paths replaced with batched/joined queries (verified by query-count assertions in tests).
- New indexes present via a single reviewed Alembic migration; `EXPLAIN` confirms index usage on the targeted queries.
- No unbounded `SELECT` without a LIMIT; list queries project only the columns the caller needs.
- Audit-log pagination returns a correct `total` with the filter pushed into SQL.

## References

- Performance audit findings 1, 2A..2C, 3A..3E, 4A..4D, 5A..5F, 6A..6B, 7A..7D, 8A (2026-05-28).
- CLAUDE.md Operating Rules #1 (correlation IDs), #5 (sole DB entry point).
