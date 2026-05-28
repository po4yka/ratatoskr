---
title: Bound background task fan-out
status: backlog
area: content
priority: high
owner: unassigned
epic: epic-eliminate-event-loop-blocking
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Bound background task fan-out #repo/ratatoskr #area/content #status/backlog ⏫

## Objective

Two background paths create an unbounded number of asyncio tasks before their concurrency semaphore, risking DB-pool exhaustion and memory growth; `github_sync` also uses `return_exceptions=False` so one failure cancels all siblings.

## Context (evidence)

- `app/tasks/github_sync.py:580` (`await asyncio.gather(*[_one(repo) for repo in repos], return_exceptions=False)` — task per repo, each grabs a DB connection in `_mark_pending` before the `asyncio.Semaphore(llm_concurrency)` at :554)
- `app/adapters/digest/analyzer.py:59-60` (gather over all posts; each calls `_cached_analysis` DB query before the semaphore at :155)

## Scope

- Gate task creation with a semaphore (wrap the whole `_one` body), or process in chunks of the configured concurrency
- Change `github_sync` gather to `return_exceptions=True` and aggregate errors

## Acceptance criteria

- [ ] Peak concurrent tasks/DB connections is bounded by the configured concurrency regardless of input size
- [ ] A single child failure does not cancel siblings
- [ ] Test with a large synthetic repo/post list confirms bounded concurrency

## Epic

Part of [[epic-eliminate-event-loop-blocking]].

## References

- Performance audit findings H-3, M-3 (2026-05-28).
