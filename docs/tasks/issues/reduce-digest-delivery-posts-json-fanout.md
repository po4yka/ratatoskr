---
title: Reduce digest delivery posts_json fanout
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

- [ ] #task Reduce digest delivery posts_json fanout #repo/ratatoskr #area/db #status/backlog 🔼

## Objective
`async_list_delivered_message_ids` loads full `posts_json` JSONB blobs for 30 days of deliveries and flattens them in Python, transferring large arrays unnecessarily.

## Context (evidence)
`app/infrastructure/persistence/digest_store.py:439-455` (`select(DigestDelivery.posts_json)` for 30 days, Python-side `delivered.update(...)`).

## Scope
Use `jsonb_array_elements_text` with `DISTINCT` to dedupe server-side, or normalize delivered post IDs into a proper join table; return only the distinct IDs.

## Acceptance criteria
- Only distinct delivered IDs cross the wire.
- No full-blob flattening in Python.
- Behavior is unchanged.
- Test added.

## Epic
Part of [[epic-fix-database-query-performance]].

## References
- Performance audit finding 7D (2026-05-28).
