---
title: Record cost_usd on structured LLM path
status: backlog
area: llm
priority: medium
owner: unassigned
epic: epic-harden-llm-cascade-reliability-and-cost
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Record cost_usd on structured LLM path #repo/ratatoskr #area/llm #status/backlog 🔼

## Objective

The `chat_structured` (Instructor) path never computes `cost_usd`, so calls routed through `PureSummaryService._summarize_with_instructor` record zero cost — skewing daily/monthly budget accounting and the hard-budget gate.

## Context (evidence)

- `app/adapters/openrouter/openrouter_client.py:655-661` — `StructuredLLMResult` captures `tokens_prompt`/`tokens_completion` but never sets `cost_usd`

## Scope

Compute `cost_usd` from token counts × per-model pricing for the structured path (reuse the pricing source used by the non-structured path), or estimate when the provider omits pricing; persist it on the `llm_calls` row.

## Acceptance criteria

- Every successful structured LLM call records a non-null `cost_usd`.
- Daily/monthly budget totals include structured-path spend.
- A test asserts cost is recorded on the structured path.

## Epic

Part of [[epic-harden-llm-cascade-reliability-and-cost]].

## References
- Performance audit finding L-1 (2026-05-28).
