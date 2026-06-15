# ADR 0015: Summarization pipeline target architecture

**Date:** 2026-06-15
**Status:** Accepted — implementation pending. Supersedes the scope of [ADR-0001](0001-no-langgraph.md).

## Context

The clean rewrite makes the **whole** summarization pipeline a LangGraph state graph (decision 2026-06-15), not just the core summarize/validate/repair cycle. This ADR specifies the end-state node graph, what existing modules collapse, and where the port boundaries sit — especially around the multi-extractor reality (the scraper chain vs the YouTube/Twitter/academic bypasses, CLAUDE.md rule 8).

## Decision

Target node graph (every node calls application **ports**, per ADR-0010/0014):

```
START → ingest → extract → ground → build_prompt → summarize → validate
          ↑                                                      │
          └──────────────── (error → lifecycle) ◄──── repair ◄──┘ (invalid)
                                                          │ (valid)
                                              enrich → persist → notify → END
```

- **`ingest`** — normalize URL, compute `dedupe_hash`, establish `correlation_id` / `request_id`.
- **`extract` is a single graph node calling an `extraction` port.** The port's adapter dispatches by `source_kind` to the scraper chain OR the YouTube/Twitter/academic extractors. **The scraper-chain fallback algorithm stays inside its adapter** — it is a cohesive algorithm, not orchestration; we do **not** explode each rung into a node (over-decomposition). "Extraction is in the graph" means the extract *step* is a node; the chain internals remain an adapter.
- **`ground`** — RAG retrieval (ADR-0005/0012) via the `retrieval` port; no-op if disabled/unavailable.
- **`summarize`** — structured output via `instructor` behind the `llm_client` port (ADR-0006).
- **`validate` → `repair` ↺ `validate`** — bounded by `repair_attempts` + `recursion_limit` (ADR-0011).
- **`enrich`** — optional two-pass enrichment; **`persist`** — `llm_calls` (incl. `attempt_trigger="graph_node"`) + `summaries`; **`notify`** — interaction/Telegram update.
- The `url_processor` / `interactive_summary_service` / `pure_summary_service` indirection **collapses** into these nodes + ports; its reusable logic (budget, semaphore, sticky-fallback, two-pass, persistence) moves into nodes or the services the ports wrap.
- State, serialization, failure→lifecycle, and observability follow ADR-0011; streaming follows [ADR-0017](0017-streaming-under-the-graph.md).

## Consequences

- One coherent orchestration replaces three layers of indirection; each node is independently unit-testable (plain `async def(state, deps)`).
- The `extraction` port unifies the chain + platform extractors behind one seam.
- Cutover is hard (ADR-0013); the parity test must cover **every** `source_kind` through the new `extract` node.

## Alternatives rejected

- **Graphify only the core cycle** — a half-measure (the original 0001 scope), rejected by the clean-rewrite decision.
- **Explode the scraper chain into per-rung nodes** — over-decomposition; the chain is an algorithm, not a workflow.
