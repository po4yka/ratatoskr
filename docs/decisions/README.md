# Architecture Decision Records

Records of consequential, non-obvious architectural and policy decisions. Reach for an ADR when a choice would otherwise be re-litigated or silently reversed.

## Convention

- **One decision per file.** Architectural decisions are numbered `NNNN-kebab-slug.md` (zero-padded). Time-boxed policy memos may instead use a dated `YYYY-MM-DD-slug.md` name.
- **Numbers are immutable IDs and are never reused.** A deleted ADR leaves a permanent gap — do not renumber, do not refill.
- Each ADR carries: a `# ADR NNNN: <title>` heading, **Date**, **Status**, **Context**, **Decision**, **Consequences** (optional: Boundary / Guardrails / Alternatives rejected).
- **Statuses:** `Proposed` (under discussion) · `Accepted` (in force) · `Reversed` (decision undone, rewritten in place — see 0001) · `Superseded by NNNN` (replaced by a later ADR).
- **Supersession:** prefer writing a new ADR that supersedes the old. Rewrite-in-place only for a clean reversal where git history is sufficient record.

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-no-langgraph.md) | LangGraph/LangChain — removed, then re-adopted | **Reversed** (2026-06-15) |
| [0002](0002-test-triage-202606.md) | Test triage 2026-06 | Accepted (rev. 2026-06-15) |
| 0003 | *retired — single-tenant simplification (deleted 2026-06-15; superseded by 0008)* | **Deleted** |
| [0004](0004-langgraph-checkpoint-persistence.md) | LangGraph checkpoint persistence (psycopg3 ↔ asyncpg) | Accepted — impl pending |
| [0005](0005-rag-grounding-policy.md) | RAG grounding policy for summarization | Accepted — impl pending |
| [0006](0006-llm-structured-output-instructor.md) | LLM structured output — keep `instructor` | Accepted |
| [0007](0007-embedding-provider-stability.md) | Embedding-provider stability & reindex | Accepted |
| [0008](0008-expansion-over-single-tenant.md) | Posture — expansion over single-tenant simplification | Accepted |
| [0009](0009-hosted-mcp-external-exposure.md) | Hosted MCP external exposure (in principle) | Accepted in principle |
| [0010](0010-graph-orchestration-layering.md) | Graph orchestration — layering & node→port boundary | Accepted — impl pending |
| [0011](0011-graph-runtime-contract.md) | Graph runtime contract — state, failure, observability | Accepted — impl pending |
| [0012](0012-cocoindex-boundary-rag-freshness.md) | CocoIndex boundary & read-your-writes RAG freshness | Accepted — impl pending |
| [0013](0013-graph-rollout-legacy-migration.md) | Graph cutover & legacy summarize-path removal | Accepted — impl pending |
| [0014](0014-ports-and-adapters-standard.md) | Ports-and-adapters as the enforced project standard | Accepted — refactor pending |
| [0015](0015-summarization-pipeline-target-architecture.md) | Summarization pipeline target architecture | Accepted — impl pending |
| [0016](0016-unified-retrieval-subsystem.md) | Unified retrieval subsystem | Accepted — impl pending |
| [0017](0017-streaming-under-the-graph.md) | Streaming under the graph | Accepted — impl pending |
| [0018](0018-refactor-strategy-and-invariants.md) | Refactor strategy & invariants | Accepted |
| [2026-05-17](2026-05-17-auth-security-second-wave.md) | Auth/security second-wave scope (memo) | **Decided** (2026-06-15) |

> Note: 0003's one load-bearing conclusion (keep `user_id` filters as an IDOR guard) is preserved as CLAUDE.md operating rule 12; the strategic reason it was retired is recorded in ADR-0008.
