---
title: "Epic: Harden LLM cascade reliability and cost controls"
kind: epic
status: backlog
area: llm
priority: critical
owner: unassigned
blocks: []
blocked_by: []
children:
  - add-hard-per-request-llm-call-cap
  - share-llm-concurrency-semaphore-batch-and-global
  - record-cost-usd-on-structured-llm-path
  - use-token-based-truncation-and-right-size-max-tokens
  - make-summary-cache-model-agnostic
  - surface-effective-llm-timeout-and-strengthen-repair
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Epic: Harden LLM cascade reliability and cost controls #repo/ratatoskr #area/llm #status/backlog #epic 🔺

## Objective

The LLM flow has strong building blocks (hard daily-budget gate, per-model circuit breaker, connection-pooled clients, transport retries scoped to connection errors). But the retry layers nest multiplicatively with no absolute per-request ceiling: `models_in_chain (5) × max_retries+1 (4) × tenacity transport retries (3)`, plus a full-cascade JSON-repair pass and a 2× sticky-fallback outer loop — a theoretical ceiling of 60–120 HTTP calls for a single URL. On a degraded-provider day this is an uncapped cost and latency blowout. Secondary gaps: cost is not recorded on the Instructor structured path (budget accounting under-counts), truncation uses char-count not token-count, and the summary cache is keyed by model name so fallback hits never serve the primary.

## Why this is an epic

All children harden the same subsystem — the OpenRouter cascade and its retry/repair/cost machinery — and share one verification surface (bounded calls per request, accurate `llm_calls` cost accounting, correct cache hits). The hard cap (B1) is the keystone; the rest are independent reliability and cost-accuracy fixes.

## Child tasks

- [[add-hard-per-request-llm-call-cap]] — CR-2: no absolute bound on LLM HTTP calls per request
- [[share-llm-concurrency-semaphore-batch-and-global]] — H-4: two independent semaphores stack to ~16 concurrent calls
- [[record-cost-usd-on-structured-llm-path]] — L-1: `chat_structured` path never records `cost_usd`
- [[use-token-based-truncation-and-right-size-max-tokens]] — M-3/M-4: 4096 `max_tokens` floor + char-based truncation guard
- [[make-summary-cache-model-agnostic]] — M-7: Redis summary cache keyed by `model_name`
- [[surface-effective-llm-timeout-and-strengthen-repair]] — M-1/M-2: silent timeout expansion + weak `json_object` repair

## Definition of done

- All child tasks closed.
- A single request has a provable, logged upper bound on total LLM HTTP calls.
- `llm_calls.cost_usd` is non-null on every successful call regardless of code path; daily/monthly budget accounting is accurate.
- Truncation decisions are token-based; `max_tokens` is right-sized for short content.

## References

- Performance audit findings CR-2, H-4, M-1..M-4, M-7, L-1 (2026-05-28).
- `app/adapters/content/pure_summary_service.py` (`_summarize_with_instructor`), `app/adapters/content/llm_response_workflow_repair.py`, `app/adapters/content/llm_response_workflow_execution.py`, `app/config/llm.py`.
