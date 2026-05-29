---
title: Bound and project summary list queries
status: backlog
area: db
priority: medium
owner: unassigned
epic: epic-fix-database-query-performance
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-29
---

- [ ] #task Bound and project summary list queries #repo/ratatoskr #area/db #status/backlog 🔼

## Status

**Partially done.** The safe, non-breaking part (5A — collapsing the two pagination
COUNT round-trips into one `count(*) FILTER (...)` query) is implemented. The
remaining audit recommendations (5B, 5C, 7A, 7B) were investigated and found to
be **breaking as originally specified**, because `summaries.json_payload` is
load-bearing on every path the audit wanted to bound/project:

- `build_summary_context` (smart collections) reads title, `topic_tags`,
  `summary_1000`/`summary_250`, `source_type`, `reading_time` straight out of
  `json_payload` to evaluate rules — projecting it away changes which summaries
  match, and lowering `async_list_user_summaries_with_request`'s 10000 limit
  silently stops matching older summaries (5C).
- `async_get_all_for_user` is the **full-snapshot sync** source
  (`app/api/services/sync/adapters.py`); a LIMIT would truncate the client sync
  (data loss) and dropping JSONB would ship empty summaries (5B/7B).
- The summary **list** view title also lives in `json_payload`, so the projection
  in 7A would blank out titles in the UI.

## Remaining work (the real fix)

Denormalize the fields these paths actually need (title, topic_tags,
source_type, reading_time) into indexed columns on `summaries` (or a
`summary_meta` table), backfilled by migration, then:

- project those columns for the list view and smart-collection evaluation
  instead of loading `json_payload`;
- keep the sync snapshot reading full payloads (it must), but it can stream/page.

This is a cross-cutting change (migration + backfill + sync protocol + smart
collection rewire) and touches the freeze-priority Mobile API contract, so it is
tracked here rather than bundled into the index/N+1 work.

## References

- Performance audit finding 5A (done), 5B/5C/7A/7B (deferred, see above) (2026-05-28).
