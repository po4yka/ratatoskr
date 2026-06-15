# ADR 0018: Refactor strategy & invariants

**Date:** 2026-06-15
**Status:** Accepted.

## Context

The committed clean rewrite is large and cross-cutting — the full summarize pipeline becomes a graph (ADR-0015), ports go project-wide (ADR-0014), retrieval unifies (ADR-0016), and the legacy path is hard-cut (ADR-0013). A big rewrite risks dropping load-bearing rules and accumulating flag/debt. This ADR governs **how** the rewrite proceeds, so every step stays safe and reversible.

## Decision

### Invariants — must hold at every step (no PR may break them)

- Correlation IDs are sacred; every user-facing error carries `Error ID: <correlation_id>`.
- Persist-everything: `crawl_results`, `llm_calls` (incl. failures, with `attempt_index` / `attempt_trigger`), `summaries`, `telegram_messages`.
- Redact `Authorization` before logging or persistence.
- `app/db/session.py::Database` is the sole asyncpg entry point (psycopg3 only for the checkpointer, ADR-0004).
- Async-only in the request path; `en` + `ru` prompts changed in lockstep.
- **Models come only from `ratatoskr.yaml`** — no code defaults (CLAUDE.md rule 11).
- `user_id` / `user_scope` IDOR filters stay (CLAUDE.md rule 12 / ADR-0008).
- B006 / B023 never suppressed project-wide.

### Strangler-fig sequencing

Introduce port → route the new path through it → migrate existing callers → enforce with an import-linter contract → delete the old code. **Each step is an independently-green PR** (lint / type / import-linter / tests / coverage ≥ floor). No long-lived rewrite branch, no big-bang PR.

### Parity safety net

Golden / parity tests are the gate for every behavior-preserving replacement — graph vs legacy (ADR-0013), retrieval unification (ADR-0016), pipeline collapse (ADR-0015). A replacement merges only when its parity test is green.

### Feature-flag lifecycle

Every migration flag (`SUMMARIZE_GRAPH_ENABLED`, `SUMMARIZE_RAG_ENABLED`, `LANGGRAPH_CHECKPOINT_ENABLED`, …) is **transitional** and is recorded with an explicit removal trigger. Flags are deleted at their cutover; **no flag outlives its migration** — preventing the dead-flag debt the recent cleanup removed.

### Definition of done (per milestone)

Behavior parity proven → old path deleted → flags removed → docs / CLAUDE / skills updated → CI green including the new import-linter contracts.

## Consequences

- The rewrite is a sequence of small, reversible, independently-green steps with a parity gate — auditable and safe to pause/resume.
- The invariants are the non-negotiable acceptance criteria for every step; reviewers check them explicitly.
