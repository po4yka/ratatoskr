# ADR 0013: Graph cutover & legacy summarize-path removal

**Date:** 2026-06-15
**Status:** Implemented (T9 cutover) — the graph is the sole summarize path; the legacy `url_processor` / `pure_summary_service` / `interactive_summary_service` files and the transitional `SUMMARIZE_GRAPH_ENABLED` flag are deleted.

## Context

The LangGraph summarize graph (ADR-0001 / [ADR-0015](0015-summarization-pipeline-target-architecture.md)) replaces the legacy `app/adapters/content/pure_summary_service.py` path (plus the `url_processor` / `interactive_summary_service` indirection), which carries battle-tested call-budget, semaphore, sticky-failure-fallback, two-pass-enrichment, and persistence semantics. The project has committed to a **clean rewrite** (2026-06-15), so the migration is decisive rather than a long coexistence.

## Decision

- **Hard cutover, parity-gated.** Build the graph path, prove behavior parity, then **replace** the legacy path in the same milestone — **no prolonged dual-path coexistence**.
- `SUMMARIZE_GRAPH_ENABLED` exists only **transiently** during development, to run both paths for the parity comparison. It is removed at cutover; it is never shipped as a long-lived production toggle (flag lifecycle per [ADR-0018](0018-refactor-strategy-and-invariants.md)).
- **Parity gate (the single safety net):** a fixture/golden parity test must show graph output ≡ legacy across **all `source_kind`s** (generic URL, YouTube, Twitter, academic, forwarded) and the budget / sticky-fallback / two-pass behaviors, before cutover.
- **Cutover sequence:** land every `source_kind` through the new `extract` node behind the transitional flag → get parity green for each → then flip the default and **delete legacy in one step**.
- **End-state:** `pure_summary_service` and the `url_processor` / `interactive_summary_service` indirection (per ADR-0015) are deleted at cutover; the graph is the single summarize path.

## Consequences

- Minimal coexistence window → less dual-maintenance, but the **parity test carries all the safety** and must be comprehensive before cutover.
- A failed post-cutover discovery means a forward fix on the graph path (the legacy path is gone) — acceptable given the parity gate, and mitigated by the checkpoint resumability (ADR-0004) and the strangler-fig sequencing (ADR-0018).
- Forces the parity test to be written first and to cover every extractor path.

## Alternatives rejected

- **Keep both paths permanently** — perpetual dual-maintenance and drift; contradicts the clean-rewrite mandate.
- **Long parity + soak coexistence** (the original conservative 0013) — superseded by the committed decision to cut over decisively.
