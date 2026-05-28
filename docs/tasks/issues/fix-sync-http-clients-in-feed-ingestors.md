---
title: Fix sync HTTP clients in feed ingestors
status: backlog
area: content
priority: critical
owner: unassigned
epic: epic-eliminate-event-loop-blocking
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Fix sync HTTP clients in feed ingestors #repo/ratatoskr #area/content #status/backlog 🔺

## Objective

The HN and Reddit feed ingesters construct the synchronous `httpx.Client` and call `.get()` from inside `async def fetch()`, blocking the entire event loop. The HN ingester runs up to 31 sequential blocking calls (1 listing + up to 30 item fetches) — worst case ~600s of total event-loop freeze affecting every Telegram handler, API request, LLM stream, and DB query.

## Context (evidence)

- `app/adapters/ingestors/hn.py:46` (`self.client = client or httpx.Client(timeout=20.0)`)
- `app/adapters/ingestors/hn.py:62` (`async def fetch`)
- `app/adapters/ingestors/hn.py:79-80` (sync `_get_json` → `self.client.get(url)`)
- `app/adapters/ingestors/hn.py:68-72` (sequential per-item loop)
- `app/adapters/ingestors/reddit.py:73` (sync client)
- `app/adapters/ingestors/reddit.py:91,94` (`async def fetch` → sync `self.client.get`)

## Scope

- Replace `httpx.Client` with `httpx.AsyncClient` in both HN and Reddit ingestors
- Make `_get_json` async and `await` the gets
- Fan out the HN per-item fetches with `asyncio.gather` behind a small semaphore (e.g. 5)
- Inject/reuse a pooled client rather than constructing per call

## Acceptance criteria

- [ ] No synchronous `httpx.Client` remains in `app/adapters/ingestors/`
- [ ] HN item fetches run concurrently with a bounded semaphore
- [ ] An async test asserts the ingester does not block the loop (e.g. a concurrent timer coroutine is not starved)

## Epic

Part of [[epic-eliminate-event-loop-blocking]].

## References

- Performance audit finding CR-1 (2026-05-28).
