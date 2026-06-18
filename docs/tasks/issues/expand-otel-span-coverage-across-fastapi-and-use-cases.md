---
title: Expand OpenTelemetry span coverage across FastAPI and application use-cases
status: backlog
area: observability
priority: low
owner: unassigned
blocks: []
blocked_by: []
created: 2026-05-17
updated: 2026-05-17
---

- [ ] #task Expand OpenTelemetry span coverage across FastAPI and application use-cases #repo/ratatoskr #area/observability #status/backlog 🔽

## Objective

`app/observability/otel.py:36-86` initializes OTEL correctly and exports to Tempo (`ops/monitoring/tempo-config.yml`), but `grep -l 'get_tracer\|start_as_current_span'` finds only 4 callers in `app/adapters/` and zero in `app/api/routers/`. The request lifecycle (auth → route → use_case → repository) is not traced, so OTEL is wired but the trace tree is mostly empty; distributed debugging falls back to correlation-id grep which doesn't show timing.

## Context

- OTEL init: `app/observability/otel.py:36-86`.
- Tempo config: `ops/monitoring/tempo-config.yml`.
- Taskiq middleware propagates W3C context (`app/tasks/middleware.py:57-108`) so worker spans exist when callers create them — but few do.

## Scope

- Auto-instrument FastAPI via `opentelemetry-instrumentation-fastapi` in `init_tracing()`.
- Add manual spans in: - `app/application/use_cases/` for each top-level use case. - `app/db/session.py` session / transaction boundaries. - `app/adapters/openrouter/` per chat completion. - `app/adapters/content/scraper/chain.py` per provider attempt (link this with the persisted scraper attempt log).
- Document `OTEL_ENABLED=true`, exporter env vars, and a Tempo Grafana dashboard panel in `docs/explanation/observability-strategy.md`.

## Acceptance criteria

- [ ] A request from Telegram or API yields a complete trace tree in Tempo (route → use case → repo → external call).
- [ ] Span attributes include `correlation_id` and `user_id`.
- [ ] No noticeable latency regression (< 5%) from instrumentation.
- [ ] Observability strategy doc updated with example trace.

## References

- OTEL init: `app/observability/otel.py:36-86`
- Tempo: `ops/monitoring/tempo-config.yml`
