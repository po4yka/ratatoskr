---
title: Cache prompt file reads on hot paths
status: backlog
area: llm
priority: high
owner: unassigned
epic: epic-eliminate-event-loop-blocking
blocks: []
blocked_by: []
created: 2026-05-28
updated: 2026-05-28
---

- [ ] #task Cache prompt file reads on hot paths #repo/ratatoskr #area/llm #status/backlog ⏫

## Objective

Prompt text files are re-read from disk on every LLM invocation across the agents and the summary service, the `PromptManager` re-hashes the file (full read_bytes + SHA256) on every cache lookup, and few-shot examples are randomly re-selected per call — which both adds hot-path I/O and defeats provider-side prompt caching.

## Context (evidence)

- `app/adapters/content/pure_summary_service.py:263,324` (read_text per call)
- `app/agents/relationship_analysis_agent.py:545,547`
- `app/agents/web_search_agent.py:228,231`
- `app/agents/combined_summary_agent.py:269,271`
- `app/agents/multi_source_aggregation_agent.py:948,950`
- `app/agents/repo_analysis_agent.py:338`
- `app/prompts/manager.py:248-250` (file hash re-read every lookup)
- `app/prompts/manager.py:318-321` (random.sample few-shots per call)
- `app/adapters/content/quality_filters.py:155-158` (lazy read on first quality check)

## Scope

- Load prompt text once (module-level or `@lru_cache`) per file
- Replace per-lookup re-hash with a startup/TTL-based invalidation
- Make few-shot selection deterministic (or cache the rendered prompt) so the provider prompt-cache fingerprint is stable

## Acceptance criteria

- [ ] Prompt files are read at most once per process per file under steady load
- [ ] Few-shot example order is stable for identical inputs
- [ ] A microbench shows no per-request prompt disk I/O

## Epic

Part of [[epic-eliminate-event-loop-blocking]].

## References

- Performance audit findings H-4, M-5, M-7, L-2 (2026-05-28).
