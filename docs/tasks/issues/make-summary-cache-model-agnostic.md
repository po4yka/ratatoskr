---
title: Make summary cache model-agnostic
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

- [ ] #task Make summary cache model-agnostic #repo/ratatoskr #area/llm #status/backlog 🔼

## Objective

The Redis summary cache key includes `model_name`, so when a fallback model produces a summary, the next request misses the primary-model cache and may re-run the LLM — doubling cost when model availability fluctuates.

## Context (evidence)

- `app/adapters/content/llm_summarizer_cache.py` — `LLMSummaryCache` key = `(prompt_version, model_name, lang, url_hash)`, TTL 7200s

## Scope

Make the cache key model-agnostic (key on `(prompt_version, lang, url_hash)` and store the producing model in the cached value), or add a model-agnostic lookup layer that checks for any cached result before falling back to a model-keyed entry; preserve prompt-version invalidation.

## Acceptance criteria

- A summary cached by a fallback model is served on the next request regardless of which model is currently primary.
- Prompt-version changes still invalidate the cache as before.
- A test covers the fallback-then-primary scenario and confirms a cache hit occurs.

## Epic

Part of [[epic-harden-llm-cascade-reliability-and-cost]].

## References
- Performance audit finding M-7 (2026-05-28).
