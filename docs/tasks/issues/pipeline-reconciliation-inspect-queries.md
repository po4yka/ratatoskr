---
title: Pipeline reconciliation inspect queries
status: backlog
area: observability
priority: medium
owner: unassigned
epic: epic-optimize-vector-and-embedding-pipeline
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Pipeline reconciliation inspect queries #repo/ratatoskr #area/observability #status/backlog 🔼

## Objective

`SummaryVectorIndexedEntityAdapter.inspect` issues 6 sequential SELECTs plus a Qdrant scroll, and the adapters run sequentially — ~7 serial round-trips per scheduled reconcile run (~350ms wasted at 50ms latency).

## Context (evidence)

`app/infrastructure/vector/reconciliation.py:232-306` (6 sequential SELECTs + scroll in `inspect`); `:144` (`for adapter in self._adapters` sequential, not `asyncio.gather`).

## Scope

Run the independent inspect queries concurrently with `asyncio.gather` (each short-lived) or merge into fewer SQL statements; run summary + repository adapters concurrently.

## Acceptance criteria

- Reconcile inspect wall-time drops materially.
- Results are unchanged.
- The adapters run concurrently.

## Epic

Part of [[epic-optimize-vector-and-embedding-pipeline]].

## References

- Performance audit finding V-5 (2026-05-28).
