# ADR 0005: RAG grounding policy for summarization

**Date:** 2026-06-15
**Status:** Accepted — implementation pending with [ADR-0001](0001-no-langgraph.md).

## Context

We expand CocoIndex/Qdrant from a write-side vector sync into a read-side **RAG retrieval layer** that grounds new summaries with related prior content (a node in the LangGraph summarize graph). This changes summary outputs and adds retrieval/LLM cost, so the contract must be explicit rather than emergent.

## Decision

- **Retrieval primitive:** embed the article text via the existing embedding service, query Qdrant for top-k (`RAG_TOP_K`, small — default 5) related prior summaries, and hydrate from Postgres. Reuse the existing embedding + Qdrant clients (no `langchain-qdrant`). This adds the one missing read-side helper (text-query → hydrated docs).
- **Scope filter is mandatory.** Every RAG query MUST apply the `user_scope` / `environment` filter — the same isolation the write path uses. Even single-tenant, this is non-negotiable defense-in-depth, consistent with CLAUDE.md rule 12.
- **Injection contract.** Retrieved context is added to the summary system prompt as clearly-delimited *"related prior summaries (reference only)"*. It MUST NOT be summarized as if it were the source, and MUST NOT introduce facts or cross-references absent from the source article (anti-contamination guard, enforced by the summary contract validation).
- **Best-effort.** If Qdrant is unavailable or returns nothing, the node is a no-op and summarization proceeds ungrounded (matches existing graceful degradation).
- **Cost.** The retrieval embedding is cheap and does NOT count against `llm_max_calls_per_request`. If grounding ever triggers an extra LLM call, that call DOES count against the budget.
- **Flag:** `SUMMARIZE_RAG_ENABLED`, default OFF.

## Consequences

- Summary outputs become dependent on corpus state. Parity tests must assert flag-off ≡ legacy output; grounded runs are intentionally non-deterministic across corpus changes.
- Privacy: grounding only ever surfaces the owner's own prior summaries (scope filter); there is no cross-tenant exposure path.

## Alternatives rejected

- **Ground from raw crawl text** — noisier and token-heavy; start with summaries, revisit if recall is poor.
- **RAG as a separate Q&A endpoint** — a different feature; out of scope for this ADR (which is specifically about grounding the summarize loop).
