---
title: "Epic: Eliminate event-loop blocking in async paths"
kind: epic
status: backlog
area: content
priority: critical
owner: unassigned
blocks: []
blocked_by: []
children:
  - fix-sync-http-clients-in-feed-ingestors
  - offload-blocking-backup-io-to-threads
  - cache-prompt-file-reads-on-hot-paths
  - bound-background-task-fanout
  - pool-httpx-clients-across-adapters
  - cap-concurrent-stealth-browser-launches
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Epic: Eliminate event-loop blocking in async paths #repo/ratatoskr #area/content #status/backlog #epic 🔺

## Objective

CLAUDE.md Operating Rule #6 is "Async only — no blocking calls in the request path." The performance audit found that rule violated in several places, including a confirmed CRITICAL: the HN and Reddit feed ingesters use the synchronous `httpx.Client` inside `async def fetch()`, freezing the entire event loop (all Telegram handlers, API requests, LLM streams, DB queries) for the duration of up to 31 sequential blocking HTTP calls. This epic removes blocking I/O from async paths and bounds unbounded task fan-out.

## Why this is an epic

The findings share one root cause (synchronous I/O or unbounded coroutine spawning on the event loop) and one verification surface (no blocking call or unbounded `gather` survives in a hot path). They are independent fixes but belong to the same correctness invariant, so grouping keeps them from being lost as scattered chores.

## Child tasks

- [[fix-sync-http-clients-in-feed-ingestors]] — CR-1: sync `httpx.Client` in `async def fetch()` (hn.py, reddit.py)
- [[offload-blocking-backup-io-to-threads]] — H-1/H-2/L-1: `pg_dump` subprocess + `read_bytes` + cleanup scan on the loop
- [[cache-prompt-file-reads-on-hot-paths]] — H-4/M-5/M-7/L-2: `read_text()` per LLM call + PromptManager re-hash + random few-shot
- [[bound-background-task-fanout]] — H-3/M-3: unbounded `asyncio.gather` over user-controlled lists
- [[pool-httpx-clients-across-adapters]] — H-5/M-4/M-5/L-3/L-4: per-call `AsyncClient` construction (TCP/TLS overhead, one leak)
- [[cap-concurrent-stealth-browser-launches]] — M-6: uncapped Playwright stealth browser launches

## Definition of done

- All child tasks closed.
- A grep/CI sweep finds no synchronous `httpx.Client`, `requests.`, blocking `read_bytes`/`read_text`, or `subprocess.run` on an `async def` hot path (or each is justified + wrapped in `asyncio.to_thread`).
- No `asyncio.gather` spawns an unbounded number of tasks over a user-controlled list.

## References

- Performance audit findings CR-1, H-1..H-5, M-3..M-7, L-1..L-4 (2026-05-28).
- CLAUDE.md Operating Rule #6 (async only).
- Verified: `app/adapters/ingestors/hn.py:46,62,79`, `app/adapters/ingestors/reddit.py:73,91,94`.
