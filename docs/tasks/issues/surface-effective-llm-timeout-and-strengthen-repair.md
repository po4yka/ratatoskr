---
title: Surface effective LLM timeout and strengthen repair JSON mode
status: backlog
area: llm
priority: low
owner: unassigned
epic: epic-harden-llm-cascade-reliability-and-cost
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Surface effective LLM timeout and strengthen repair JSON mode #repo/ratatoskr #area/llm #status/backlog 🔽

## Objective

`effective_llm_timeout` silently expands past the configured `LLM_CALL_TIMEOUT_SEC` (465s vs 420s with defaults) with no log at the call site, surprising operators; and the JSON-repair call uses the weaker `json_object` mode through the full cascade, increasing fallback churn.

## Context (evidence)

- `app/adapters/content/llm_response_workflow_execution.py:318-334` — `effective_llm_timeout = max(llm_timeout, num_models*per_model_timeout+15)`; the 15s buffer is hardcoded and the expansion is never logged
- `app/adapters/content/llm_response_workflow_repair.py:141` — repair uses `json_object` format rather than a structured/schema-enforced mode

## Scope

Log the computed effective timeout and its derivation (configured value, per-model contribution, buffer) at WARNING level whenever it exceeds the configured `LLM_CALL_TIMEOUT_SEC`; consider using structured/schema-enforced output for the repair call instead of bare `json_object`; document the effective-timeout derivation in the LLM timeout env reference.

## Acceptance criteria

- A WARNING log is emitted whenever `effective_llm_timeout` differs from the configured value, including the derivation.
- The repair call uses the strongest available JSON enforcement mode.
- `docs/reference/environment-variables.md` documents how the effective timeout is derived from `LLM_CALL_TIMEOUT_SEC`, `LLM_PER_MODEL_TIMEOUT_MIN_SEC`, and the hardcoded buffer.

## Epic

Part of [[epic-harden-llm-cascade-reliability-and-cost]].

## References
- Performance audit findings M-1, M-2 (2026-05-28).
