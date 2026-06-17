---
title: Add vector reconciler metrics and staleness-lag alert
status: backlog
area: observability
priority: high
owner: unassigned
blocks: []
blocked_by: []
created: 2026-05-17
updated: 2026-05-17
---

- [ ] #task Add vector reconciler metrics and staleness-lag alert #repo/ratatoskr #area/observability #status/backlog ⏫

## Objective

`reconcile_vector_index` is the steady-state guarantor that Qdrant summary and repository vectors don't drift from Postgres. The task returns a `ReconcileSummary(scanned, requeued, skipped, failed)` but never emits metrics, so if the Taskiq job silently stops (lock leak, Redis outage, exception in `_fetch_stale_summaries`) semantic search degrades with no visible signal until users complain.

## Context

- Task body: `app/tasks/reconcile_vector_index.py:75-136` — returns `ReconcileSummary`, never calls `prometheus_client`.
- `rg "vector_reconcile" app/observability/ ops/monitoring/` is empty.
- Cron interval is controlled by `VECTOR_RECONCILE_CRON` (default `*/30 * * * *`) per `CLAUDE.md` env reference.

## Scope

- New counter `ratatoskr_vector_reconcile_rows_total{outcome}` (`scanned`, `requeued`, `skipped`, `failed`).
- New gauge `ratatoskr_vector_reconcile_oldest_lag_seconds` — `max(now - last_indexed_at)` across stale rows; updated each run.
- New counter `ratatoskr_vector_reconcile_runs_total{status}` (`success`, `error`).
- Prometheus alert: `oldest_lag_seconds > 2 * VECTOR_RECONCILE_CRON_INTERVAL` for 15m → severity warning.
- Prometheus alert: no increment on any outcome for 2 cron periods → severity critical (task stopped).

## Acceptance criteria

- [ ] All three metrics registered and incremented per run.
- [ ] Two alert rules in `ops/monitoring/alerting_rules.yml` with runbook links.
- [ ] Unit test asserts metrics increment on a forced run with N stale rows.

## References

- Task: `app/tasks/reconcile_vector_index.py:75-136`
- Vector sync doc: `docs/vector-index-sync.md`
- Cron config: `VECTOR_RECONCILE_CRON` (see `CLAUDE.md` §"Quick Reference: Environment Variables")
