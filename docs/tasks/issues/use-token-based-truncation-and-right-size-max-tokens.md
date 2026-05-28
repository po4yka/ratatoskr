---
title: Use token-based truncation and right-size max_tokens
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

- [ ] #task Use token-based truncation and right-size max_tokens #repo/ratatoskr #area/llm #status/backlog 🔼

## Objective

The `max_tokens` floor of 4096 is applied even to tiny content (wasted budget, padding temptation), and the long-content truncation guard uses character count not token count — so CJK/Cyrillic content can overflow a model's context window after "passing" the guard.

## Context (evidence)

- `app/adapters/content/pure_summary_service.py:427-454` — `dynamic_budget = max(4096, ...)`, 4096 floor applied unconditionally
- `app/adapters/content/pure_summary_service.py:47-59` — truncation guard uses `len(content_text)` in characters, while `count_tokens` is available at line 430

## Scope

Lower the `max_tokens` floor for short content (e.g. 512–1024) scaled to input; make the truncation guard token-based using the same tokenizer as `count_tokens`.

## Acceptance criteria

- Short content gets a proportionally smaller output budget instead of the 4096 floor.
- Truncation decisions are token-based, not character-based.
- A Cyrillic or CJK fixture that previously passed the character guard but would overflow context now triggers truncation correctly.

## Epic

Part of [[epic-harden-llm-cascade-reliability-and-cost]].

## References
- Performance audit findings M-3, M-4 (2026-05-28).
