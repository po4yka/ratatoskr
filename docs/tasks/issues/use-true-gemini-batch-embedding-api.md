---
title: Use true Gemini batch embedding API
status: backlog
area: llm
priority: high
owner: unassigned
epic: epic-optimize-vector-and-embedding-pipeline
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Use true Gemini batch embedding API #repo/ratatoskr #area/llm #status/backlog ⏫

## Objective

`generate_embeddings_batch` fires N concurrent single-text requests via `asyncio.gather` with no rate limit or backoff, instead of using the Gemini `BatchEmbedContentsRequest` (up to 100 texts/call). During backfill this hits rate limits (429 storm) and wastes per-request overhead.

## Context (evidence)

`app/infrastructure/embedding/gemini_embedding_service.py:87-99` (`asyncio.gather(*(self.generate_embedding(t, ...) for t in texts))` — no batch endpoint, no semaphore, no retry).

## Scope

Use the true Gemini batch embedding endpoint, chunking into ≤100 per call; add a concurrency cap and exponential backoff/retry on 429; preserve ordering.

## Acceptance criteria

- N texts use ceil(N/100) API calls.
- Rate-limit errors are retried with backoff.
- Ordering is preserved.
- Test covers a >100-text batch.

## Epic

Part of [[epic-optimize-vector-and-embedding-pipeline]].

## References

- Performance audit finding E-2 (2026-05-28).
