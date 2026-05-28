---
title: Add hard per-request LLM call cap
status: backlog
area: llm
priority: critical
owner: unassigned
epic: epic-harden-llm-cascade-reliability-and-cost
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Add hard per-request LLM call cap #repo/ratatoskr #area/llm #status/backlog 🔺

## Objective

LLM retry layers nest multiplicatively with no absolute per-request ceiling: models_in_chain (5 default) × max_retries+1 (4) × tenacity transport retries (3), PLUS a full-cascade JSON-repair pass and a 2× sticky-fallback outer loop in PureSummaryService. Theoretical worst case is 60–120 HTTP calls for a single URL (×4 for a 4-URL batch). Happy path is 1–2 calls, so this is a tail risk, but on a degraded-provider day it is an uncapped cost/latency blowout.

## Context (evidence)

- `app/adapters/content/llm_response_workflow_execution.py:113-206` — per-attempt cascade + repair on top, no global cap across the requests list
- `app/adapters/content/llm_response_workflow_repair.py:71-207` — `_attempt_json_repair` runs full cascade again
- `app/adapters/content/pure_summary_service.py:180-215` — 2× sticky-fallback outer loop, instructor max_retries=3
- `app/config/llm.py` — 5-model default chain

## Scope

Add a hard per-request counter of total LLM HTTP calls that aborts the workflow with a clear error when exceeded; make the cap configurable via env var; ensure the repair pass and sticky-fallback loop count against the same budget; log the count on every request.

## Acceptance criteria

- A single request can never exceed the configured LLM-call cap.
- The cap default is sane for the Pi (matches the realistic worst-case happy path, not 120).
- The count is logged on every request and surfaced in `llm_calls`.
- A test forces all-fail and asserts the cap is honored and the workflow aborts cleanly.

## Epic

Part of [[epic-harden-llm-cascade-reliability-and-cost]].

## References
- Performance audit finding CR-2 (2026-05-28).
