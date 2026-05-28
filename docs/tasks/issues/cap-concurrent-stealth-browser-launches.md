---
title: Cap concurrent stealth browser launches
status: backlog
area: scraper
priority: medium
owner: unassigned
epic: epic-eliminate-event-loop-blocking
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Cap concurrent stealth browser launches #repo/ratatoskr #area/scraper #status/backlog 🔼

## Objective

The scrapling stealth fallback launches a full Playwright/Chromium browser via `run_in_executor` with no cap on concurrent launches, risking FD/RAM/CPU exhaustion when multiple basic fetches fail simultaneously.

## Context (evidence)

- `app/adapters/content/scraper/scrapling_provider.py:200` (`await loop.run_in_executor(None, _sync_fetch_stealth, url, stealth_cls)` — no semaphore around the browser launch)

## Scope

- Add a module-level `asyncio.Semaphore` (e.g. 2) guarding stealth browser launches
- Make the cap configurable (e.g. via settings)

## Acceptance criteria

- [ ] Concurrent stealth browser processes are bounded by the semaphore
- [ ] A burst of failing basic fetches does not spawn unbounded browsers
- [ ] The semaphore limit is configurable without a code change

## Epic

Part of [[epic-eliminate-event-loop-blocking]].

## References

- Performance audit finding M-6 (2026-05-28).
