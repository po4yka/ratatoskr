---
title: Share LLM concurrency semaphore across batch and global paths
status: backlog
area: llm
priority: high
owner: unassigned
epic: epic-harden-llm-cascade-reliability-and-cost
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Share LLM concurrency semaphore across batch and global paths #repo/ratatoskr #area/llm #status/backlog ⏫

## Objective

Two independent concurrency semaphores exist — the global `MAX_CONCURRENT_CALLS=4` and the per-batch `max_concurrent=4` — so a single batch can saturate the global pool while a second user queues, and total concurrent LLM calls can reach ~16. The repair call also acquires the semaphore separately.

## Context (evidence)

- `app/adapters/content/url_batch_processor.py:456-458` — batch gather under a separate `asyncio.Semaphore(max_concurrent)`
- `SummarizationRuntime` global semaphore (`MAX_CONCURRENT_CALLS=4`)
- `app/adapters/content/llm_response_workflow_repair.py:114-128` — repair acquires semaphore separately

## Scope

Share one global LLM concurrency semaphore across the interactive and batch paths via DI; ensure the repair call reuses (does not double-acquire) the request's slot; verify the DI wiring passes the same semaphore object.

## Acceptance criteria

- Total concurrent LLM calls across all paths is bounded by one global limit.
- The batch path cannot starve single-URL requests.
- The repair call reuses the already-acquired slot rather than acquiring a second one.
- Verified by a concurrency test showing the global bound is respected under concurrent batch + interactive load.

## Epic

Part of [[epic-harden-llm-cascade-reliability-and-cost]].

## References
- Performance audit finding H-4 (2026-05-28).
