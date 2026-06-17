# ADR 0001: LangGraph/LangChain — removed (2026-06-10), reversed (2026-06-15)

**Original date:** 2026-06-10
**Reversed:** 2026-06-15
**Status:** Reversed — implemented. LangGraph/LangChain has been re-adopted for the scoped summarize-graph use case (see **Reversal** below); the graph shipped as the sole summarize path at the T9 cutover (see [ADR-0013](0013-graph-rollout-legacy-migration.md)). This ADR is rewritten in place; git history preserves the original text.

## Original context (2026-06-10)

The `langgraph` optional dependency group (`pip install -e ".[langgraph]"`) pulled in `langchain`, `langgraph`, `langgraph-checkpoint-postgres`, and `psycopg[binary]`. It was added to explore LangGraph as a **durable task-execution backend** with Postgres checkpointing. A full scan of the codebase found zero imports of any of these packages — the integration was never shipped. We removed the group and added ruff `banned-api` guards to prevent silent re-introduction.

That decision was correct **for LangGraph-as-durable-task-backend**: Taskiq + Redis already covered durable task execution, and `langgraph-checkpoint-postgres` added a dedicated pool/schema for no active benefit.

## Reversal (2026-06-15)

We are re-adopting LangGraph for a **different use case the original ADR never evaluated**: orchestrating the summarize/repair workflow as a checkpointed state graph, with a RAG grounding node over the existing CocoIndex/Qdrant corpus.

### Decision

- Adopt LangGraph (+ `langgraph-checkpoint-postgres`) to orchestrate the summarization pipeline as a state graph. **Target scope (revised 2026-06-15): the whole pipeline** — extraction, RAG grounding, summarize, validate, repair, enrich, persist, notify — as graph nodes calling application ports. The end-state node architecture is specified in [ADR-0015](0015-summarization-pipeline-target-architecture.md). (The original 2026-06-15 framing scoped this to the core summarize/validate/repair cycle; the committed clean rewrite broadens it to the full pipeline.)
- Re-add the dependencies as an optional `graph` extra; the default image is unaffected unless the extra is installed.
- **Narrow — not lift entirely — the `banned-api` guard**: keep banning the kitchen-sink `langchain` monorepo and `langchain_community`; allow `langgraph` and `langchain_core`.
- Keep `instructor` for structured output (no `langchain-openai`); reuse our own embedding + Qdrant clients (no `langchain-qdrant`).

### Boundary with Taskiq (the load-bearing distinction)

- **Taskiq** remains the durable *task scheduler*: what work runs, when, job-level retries, cron. Unchanged.
- **LangGraph checkpointing** persists *graph state between nodes within a single summarize run*, so a crashed/restarted run resumes at the last completed node instead of repeating expensive LLM calls. This is intra-run resumability — a concern Taskiq does not address.

They are complementary, not duplicative. LangGraph is **not** adopted as a task scheduler or background-job system. This is why the reversal does not contradict the original rationale: the original "no" was about durable *task* execution, which Taskiq still owns.

### Guardrails

- `SUMMARIZE_GRAPH_ENABLED` is a **transitional** migration flag, not a permanent toggle: the graph becomes the single summarize path at a parity-gated **hard cutover** ([ADR-0013](0013-graph-rollout-legacy-migration.md)), after which the legacy `pure_summary_service` path and the flag are both deleted. Flag lifecycle governed by [ADR-0018](0018-refactor-strategy-and-invariants.md).
- Checkpoint tables live in a dedicated `langgraph` Postgres schema, created by `.setup()` (not Alembic), trivially droppable.
- `thread_id = correlation_id` so resumable runs preserve the sacred correlation ID.

### Consequences

- New deps under the `graph` extra: `langgraph`, `langgraph-checkpoint-postgres`, `langchain-core`, `psycopg3` + `psycopg-pool` (~30 transitive packages; record in `docs/reference/dependency-supply-chain.md`).
- A second (psycopg3) Postgres pool when checkpointing is enabled; the connection-budget math in `docs/cocoindex.md` is updated accordingly.
- Security scanners (OSV / pip-audit / Safety) must triage any `langchain*` advisories.

### Implementation status

Decision accepted 2026-06-15. Implementation is **planned but not yet merged**; it will land behind the feature flags above, phased with per-phase verification. Until it lands, the `banned-api` guard and the absence of the dependencies remain in place.

## Original rationale & consequences (retained for history)

- **Taskiq already covers durable execution.** Adding a second durable-execution framework increased operational complexity with no benefit *as a task backend*.
- **Postgres checkpointing overhead.** A dedicated schema and connection pool for no active usage.
- **Zero active usage at the time.** Dead optional dependencies inflated the dependency surface and slowed `uv lock`.

Any further change to this decision must update this ADR.
