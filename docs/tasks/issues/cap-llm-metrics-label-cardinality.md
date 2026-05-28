---
title: Cap LLM metrics label cardinality
status: backlog
area: observability
priority: high
owner: unassigned
epic: epic-fix-caching-correctness-and-metrics-cardinality
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Cap LLM metrics label cardinality #repo/ratatoskr #area/observability #status/backlog ⏫

## Objective

14 LLM metrics carry a `model` label populated with the full OpenRouter model identifier; as OpenRouter routes to a growing model catalog (fallback chain, experiments), Prometheus series grow unbounded. The circuit-breaker gauge writes 3 series per model, and `AGGREGATION_EXTRACTION` has 5 label dims including a free-form `platform`. On the Pi-hosted Prometheus this risks OOM / multi-second scrape timeouts.

## Context (evidence)

`app/observability/metrics.py:106-163` and `:820-845` (14 metrics with `model` label: LLM_CALL_ATTEMPTS_TOTAL, LLM_TOKENS_TOTAL, LLM_COST_USD_TOTAL, LLM_CALL_LATENCY_SECONDS, LLM_PARSE_FAILURES_TOTAL, LLM_REPAIR_ATTEMPTS_TOTAL, LLM_FALLBACK_ATTEMPTS_TOTAL, LLM_TIMEOUTS_TOTAL, OPENROUTER_PER_MODEL_*, OPENROUTER_CIRCUIT_BREAKER_STATE, OPENROUTER_STREAM_FALLBACK, OPENROUTER_TOKENS, OPENROUTER_LATENCY); `:830-833` (circuit breaker writes 3 series/model); `:320-327` (AGGREGATION_EXTRACTION 5 label dims incl. free-form platform); model sourced at `:927`.

## Scope

Drop or bucket the `model` label (e.g. an allowlist of tracked models → others bucketed to `other`, or move model to exemplars/logs); convert circuit-breaker state to a single integer gauge per model (0/1/2) instead of label-per-state; bound or remove the free-form `platform` label.

## Acceptance criteria

- Total Prometheus series is bounded under model churn.
- Circuit-breaker state is one gauge per model.
- A cardinality test/estimate documents the new ceiling.

## Epic

Part of [[epic-fix-caching-correctness-and-metrics-cardinality]].

## References

- Performance audit findings M-1, M-2, M-3 (2026-05-28).
