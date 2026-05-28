---
title: Pool httpx clients across adapters
status: backlog
area: content
priority: medium
owner: unassigned
epic: epic-eliminate-event-loop-blocking
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Pool httpx clients across adapters #repo/ratatoskr #area/content #status/backlog 🔼

## Objective

Several adapters construct a new `httpx.AsyncClient` per call when no pooled client is injected, paying TCP+TLS handshake each time; one path also never closes the client, creating a connection leak.

## Context (evidence)

- `app/adapters/ingestors/x_timeline.py:160` (per-call AsyncClient, aclose in finally)
- `app/adapters/openrouter/model_capabilities.py:274,297` (per-call client)
- `app/adapters/social/meta/oauth.py:257,278` (per-call client, never closed — LEAK)
- `app/adapters/ingestors/threads_user_threads.py:142`
- `app/adapters/social/meta/instagram_client.py:246`
- `app/adapters/social/meta/threads_client.py:283`

## Scope

- Inject a shared, long-lived `httpx.AsyncClient` via the constructor/DI for each adapter (the params already exist)
- Open once and close on app shutdown
- Fix the oauth leak with `async with` or a shared client

## Acceptance criteria

- [ ] No adapter constructs a new `AsyncClient` per request method when a pooled client is available
- [ ] The meta/oauth client is always closed
- [ ] Connection reuse verified (e.g. connection count does not grow under repeated calls)

## Epic

Part of [[epic-eliminate-event-loop-blocking]].

## References

- Performance audit findings H-5, M-4, M-5, L-3, L-4 (2026-05-28).
