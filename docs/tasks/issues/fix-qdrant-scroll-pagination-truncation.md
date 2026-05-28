---
title: Fix Qdrant scroll pagination truncation
status: backlog
area: observability
priority: high
owner: unassigned
epic: epic-optimize-vector-and-embedding-pipeline
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Fix Qdrant scroll pagination truncation #repo/ratatoskr #area/observability #status/backlog ⏫

## Objective

Every Qdrant `scroll()` call discards the returned `next_page_offset` and caps at 5000 points, so any deployment with >5,000 summaries/repositories silently truncates the indexed-ID set. The reconciler then reports already-indexed vectors as "missing", re-embeds them every run, and produces wrong lag metrics. This is a silent data-correctness failure.

## Context (evidence)

`app/infrastructure/vector/qdrant_store.py:489` (`records, _ = client.scroll(... limit=limit or 5000)` in `get_indexed_summary_ids`); `:532` (`get_indexed_repository_ids`); `:575`; `:622`; `:265` (`_fetch_request_point_ids`, limit 10000). The `next_page_offset` (second tuple element) is always discarded as `_`.

## Scope

Loop on `next_page_offset` until it is `None`, accumulating all records; or use the Qdrant count API where only a total is needed. Apply to all five scroll sites.

## Acceptance criteria

- `get_indexed_summary_ids`/`get_indexed_repository_ids` return the complete ID set regardless of collection size.
- A test with >5000 synthetic points returns all of them.
- The reconciler reports zero false-missing at scale.

## Epic

Part of [[epic-optimize-vector-and-embedding-pipeline]].

## References

- Performance audit finding V-1 (2026-05-28).
