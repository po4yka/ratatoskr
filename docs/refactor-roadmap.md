# Ratatoskr Refactor Roadmap — ADRs 0001–0018

**Goal.** Execute a clean, strangler-fig rewrite of the summarization pipeline onto a LangGraph `StateGraph` behind application ports, replacing the `url_processor` / `interactive_summary_service` / `pure_summary_service` / `ContentExtractor` indirection with serializable id-based graph state, a single extraction port, a single unified retrieval port, RAG grounding with read-your-writes freshness, streaming bridged through a `stream_sink` port, and Postgres checkpoint persistence — landing each track as an independently-green PR gated by a per-source-kind parity net before any legacy deletion. The end state matches ADR-0010/0011/0014/0015/0016/0017 with every invariant in ADR-0018 preserved.

ADRs: [`docs/decisions/`](decisions/) (0001 reversed: langgraph/langchain_core allowed, langchain/langchain_community still banned; 0006 keeps instructor; 0014 ports-and-adapters; 0015 pipeline target; 0018 strategy + invariants).

> **Status (as of 2026-06-17): T1–T9 DELIVERED.** The LangGraph summarize graph (`app/application/graphs/summarize/`) is the sole summarize path. Legacy files (`url_processor.py`, `pure_summary_service.py`, `interactive_summary_service.py`) are deleted. The `GraphURLProcessor` facade (`app/adapters/content/graph_url_processor.py`) is the only URL-flow entry point, wired via DI with no flag gate. `SUMMARIZE_GRAPH_ENABLED` and all T5–T9 transitional flags are retired. The per-track sections below are the historical plan; they are preserved as the design record.

---

## 1. Invariants (ADR-0018) — non-negotiable, every wave preserves

Every PR must keep all of these green. Reviewers gate on each one explicitly.

- [ ] **Correlation IDs are sacred.** `correlation_id` is the graph `thread_id`; never regenerated mid-flow; present on every log line, DB row, StreamEvent, and the user-facing `Error ID: <correlation_id>`.
- [ ] **URL normalization before dedup.** `app/core/url_utils.py` is the only normalizer; `dedupe_hash` (sha256 of normalized URL) is the idempotence key.
- [ ] **Persist everything.** `crawl_results`, `llm_calls` (success AND failure, with `attempt_index` + `attempt_trigger`), `summaries`, `telegram_messages`. Qdrant index write is best-effort; the DB row is the source of truth.
- [ ] **Redact `Authorization` headers** before persistence and logging; never serialize them into checkpoint state.
- [ ] **`Database` (`app/db/session.py`) is the sole asyncpg entry point.** The checkpointer psycopg3 pool and prune connection are the only sanctioned exceptions and must NOT route through `Database`.
- [ ] **Async only.** No blocking calls in the request path; `asyncio.to_thread` only for genuinely sync libs (e.g. qdrant-client).
- [ ] **en + ru prompts in lockstep.** Any prompt edit touches all mirrored files together (`summary_system_{en,ru}.txt`, `*_instructor.txt`, `enrichment_system_{en,ru}.txt`).
- [ ] **Model selection has no code default.** `ratatoskr.yaml` is the single source of truth; tests under `patch.dict(..., clear=True)` inject `tests/_config_env.py::MODEL_SELECTION_ENV`.
- [ ] **`user_scope` / `environment` / `user_id` IDOR filters** are always injected server-side (CLAUDE rule 12); retrieval makes omission structurally impossible.
- [ ] **B006 / B023 never suppressed** project-wide.
- [ ] **No parallel error path.** Every failure (node exception, `GraphRecursionError`, call-budget exhaustion) routes to the single terminal-failure helper → `RequestProcessingJob` terminal + `RequestStatus.ERROR` + `Error ID`.
- [ ] **Minimal/id-based checkpoint state.** `SummarizeState` holds serializable primitives only; content is re-fetched from Postgres by `request_id`. Live deps (ports, sessions, `Database`) are injected via config/`functools.partial`, never stored in state. `LANGGRAPH_STRICT_MSGPACK=true`.
- [ ] **langgraph/langchain_core confined to the graph-assembly module + `app/di/graphs.py`.** `application-no-outward` stays green (langgraph is third-party, not a layer violation).
- [ ] **No transitional flag outlives its migration.** Each flag has a recorded removal trigger and is deleted at its cutover.
- [ ] **Strangler-fig.** Each step is independently green (lint + type + import-linter + tests ≥ 80% coverage + OpenAPI drift clean). Legacy is deleted only after the parity net is green per source_kind.

---

## 2. Dependency DAG + Wave Plan

### DAG (resolved from `depends_on`)

```
T1 (deps foundation)        T3 (ports foundation)
  |                           |       \         \
  v                           |        \         \
T2 (checkpoint infra) <--T1   v         v          v
  \                         T4 (retrieval)  (T3 feeds T5,T7)
   \                          |
    \                         |
     \----> T5 (graph skeleton) <-- T2, T3
                |   \      \
                |    \      \
                v     v      v
   T6 (rag node)  T7 (extraction)  T8 (streaming)
   <-- T4,T5      <-- T5,T3        <-- T5
                \      |        /
                 \     |       /
                  v    v      v
                 T9 (parity + hard cutover + flag retirement)
                 <-- T5,T6,T7,T8
```

### Waves

| Wave | Tracks (parallel within wave) | Gate to next wave |
|---|---|---|
| **Wave 0 — Foundation** | **T1** (deps/build) → then **T3** (ports/import-linter) | T1 must land before T2/T5 import langgraph; T3 ports + `di/graphs.py` stub merged green. T1 and T3 both touch `pyproject.toml` / `.importlinter` so serialize T1→T3. |
| **Wave 1 — Infra + Skeleton + Retrieval** | **T2** (checkpoint infra), **T4** (unified retrieval) | T2 and T4 are independent (T2⊥T4); both depend only on Wave 0. Run in parallel. |
| **Wave 2 — Graph skeleton** | **T5** (graph skeleton + runtime contract) | Needs T2 (checkpointer + `graph_node` enum) and T3 (ports). T4 must also be merged so T5's `deps.py` types against the real `RetrievalPort` shape. Single track — it owns `nodes/*` and `graph.py`. |
| **Wave 3 — Node bodies + streaming** | **T6** (rag/ground + freshness), **T7** (extraction port + pipeline collapse), **T8** (streaming bridge) | All depend on T5 scaffold. They edit overlapping `nodes/*` + `di/*` + `url_processor.py` — **sequence within the wave** (T7 owns the bulk collapse; T6 and T8 coordinate the shared `build_prompt`/state seam with T7). Not truly parallel; treat as ordered T7 → {T6, T8}. |
| **Wave 4 — Cutover** | **T9** (parity net + hard cutover + flag retirement) | Needs T5–T8 complete. Parity green per source_kind, then flip default + delete legacy + retire flags in one step. |

> **Reality check baked into the plan:** as of audit, `app/application/graphs/` is absent, `app/application/ports/retrieval.py` is absent, `app/di/graphs.py` is absent, `graph_node` is NOT in `LLMAttemptTrigger`, and `pyproject.toml` banned-api still bans langgraph/langchain_core. Wave 0 (T1) lifts the ban and adds the `graph` extra; nothing downstream compiles until it lands.

---

## 3. Per-Track Sections

### T1 — Dependency & Build Foundation

**Key:** `T1-deps-foundation` · **ADRs:** 0001, 0004, 0006, 0007, 0018 · **Effort:** M · **depends_on:** none · **parallel-safe:** ❌ (shared hot file `pyproject.toml`)

**Summary.** Add an optional `graph` extra (langgraph + langgraph-checkpoint-postgres + psycopg3/psycopg-pool + a direct langchain-core pin), narrow the ruff banned-api guard to allow langgraph/langchain_core while still banning the langchain monorepo + community, relock via uv, and record the transitive supply chain. No code/runtime/default-image change — pure build scaffolding so later waves can `import langgraph`.

**Work items (ordered).**

1. `pyproject.toml [project.optional-dependencies]` (~196–317): add `graph = [...]` with `langgraph>=1.2.4,<2`, `langchain-core>=1.4.0,<2` (direct pin per ADR-0001), `langgraph-checkpoint-postgres>=2,<3` (verify floor at lock), `psycopg[binary]>=3.3.4`, `psycopg-pool>=3.2`. Keep `instructor>=1.5.0,<2` (line 185) untouched (ADR-0006). Do NOT add langchain-openai / langchain-qdrant.
2. Narrow `[tool.ruff.lint.flake8-tidy-imports.banned-api]` (112–120): REMOVE `langgraph`, `langchain_core`, `langgraph_checkpoint` bans; KEEP `langchain`, `langchain_community`. Update stale "was intentionally removed" messages to cite reversed ADR-0001. Enforcement stays via the already-selected `TID` rule (no rule-selection change).
3. Note that `psycopg[binary]` is already in the base dependencies; `psycopg-pool` is genuinely new (absent from lock).
4. `make lock-uv` (Makefile 78–83): regenerate `uv.lock`; re-export requirements{,-dev,-all}.txt. **Decision:** if `graph` must be in the test image, add `--extra graph` to the `requirements-all.txt` export (line 82) AND mirror it in `.github/workflows/ci.yml` (70–77) — they must stay byte-identical or the lockfile-drift gate fails.
5. `docs/reference/dependency-supply-chain.md`: add a section for the ~30 graph-extra transitive packages; note Safety/pip-audit/OSV must triage langchain* advisories.
6. Confirm (no-op) the `graph` extra is NOT in `ops/docker/Dockerfile` (38–39) or `Dockerfile.api` (32) — default image unaffected (ADR-0001).
7. Run `tools/scripts/check_excluded_versions.py` (auto-covers new extra) and `ruff check .`.

**Files:** `pyproject.toml`, `uv.lock`, `requirements{,-dev,-all}.txt`, `Makefile`, `.github/workflows/ci.yml`, `docs/reference/dependency-supply-chain.md`, `tools/scripts/check_excluded_versions.py`, `ops/docker/Dockerfile{,.api}`.
**New artifacts:** `graph` extra; new `uv.lock` nodes for `langgraph-checkpoint-postgres` + `psycopg-pool`; supply-chain doc section; optional `--extra graph` clause in Makefile + ci.yml.

**Risks.** Shared `pyproject.toml` collides with every dep-touching track (serialize). Relock needs `SAFETY_API_KEY` + network or the lock silently drifts to PyPI. langchain ecosystem already transitively present via scrapegraphai — a direct pin may bump shared versions; verify scrapegraphai still resolves and langchain/langchain_community stay transitive-only. Wrong `langgraph-checkpoint-postgres` floor can fail resolution. Adding `--extra graph` to one export site but not the other guarantees a drift-CI failure. Reflexively adding `graph` to a Dockerfile violates ADR-0001.

**Verification gates.**

- `uv lock --check` exits 0; `git diff` over `uv.lock` + `requirements*.txt` clean after a second `make lock-uv`.
- `uv sync --extra graph` then `python -c 'import langgraph, langchain_core; from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver; import psycopg, psycopg_pool'` succeeds.
- `ruff check .` green; throwaway `import langchain` still trips banned-api, `import langgraph` does not.
- `tools/scripts/check_excluded_versions.py` exits 0.
- Default-image extra set resolves WITHOUT pulling langgraph-checkpoint-postgres/psycopg-pool.

---

### T2 — Checkpoint Infrastructure

**Key:** `T2-checkpoint-infra` · **ADRs:** 0004, 0011, 0001, 0018, 0013 · **Effort:** L · **depends_on:** T1 · **parallel-safe:** ❌ (edits canonical `core.py` enum)

**Summary.** Stand up a dedicated psycopg3 `AsyncConnectionPool` + `AsyncPostgresSaver` (separate from the asyncpg `Database`), create the `langgraph` schema via `.setup()` at bot/API lifespan, add a nightly Redis-locked Taskiq prune for stale checkpoints, wire `LANGGRAPH_STRICT_MSGPACK`, and ship the Alembic migration adding `graph_node` to `llm_attempt_trigger`. All infra gated OFF by feature flag.

**Work items (ordered).**

1. Add `graph_node` to `LLMAttemptTrigger` StrEnum in `app/db/models/core.py` (30–56); `Enum(..., native_enum=True)` (59–65) picks it up via `values_callable`. Coordinate — canonical file edited by other tracks.
2. New migration `app/db/alembic/versions/0036_add_graph_node_attempt_trigger.py` modeled on `0027_add_webwright_tool_attempt_trigger.py`: revision `0036`, down_revision `0035`, `upgrade = op.execute("ALTER TYPE llm_attempt_trigger ADD VALUE IF NOT EXISTS 'graph_node'")`, downgrade no-op (PG16 target).
3. New `app/config/langgraph.py` (template `git_backup.py`): frozen `LangGraphCheckpointConfig` — `enabled` (`LANGGRAPH_CHECKPOINT_ENABLED`, default False), `strict_msgpack` (default True), pool min/max (**ADR-0004 authoritative max=5; document in `docs/vector-index-sync.md` connection-budget table**), `schema_name` (default `langgraph`), `dsn_override` (strips `postgresql+asyncpg://`), retention days (default 90), prune cron (nightly).
4. Wire config into `app/config/settings.py` at both aggregation sites (~205–210 dataclass, ~268–273 pydantic) and the AppConfig builder (~531–536); export from `app/config/__init__.py`.
5. New `app/infrastructure/checkpointing/runtime.py` `CheckpointerRuntime`: build a dedicated psycopg3 `AsyncConnectionPool` (autocommit, `row_factory=dict_row`, search_path = `langgraph`), wrap in `AsyncPostgresSaver`, `await saver.setup()`, expose `start()/stop()`. Strip the asyncpg DSN prefix. **Must NOT route through `Database`.**
6. Hook start/stop into both lifespans: `app/api/main.py` (same failure-isolation try/except + finally-stop pattern) and `bot.py main()` (after `db.migrate()`, shutdown in finally ~85–93). Gate on `cfg.langgraph_checkpoint.enabled`.
7. New `app/tasks/langgraph_prune.py` (template `purge_raw_data.py`): `@broker.task(task_name="ratatoskr.langgraph.prune")`, acquire `RedisDistributedLock`, open its own short-lived psycopg3 connection (NOT `Database`), DELETE from `langgraph.checkpoints/checkpoint_blobs/checkpoint_writes` past retention; early-return if flag off.
8. Register in `app/tasks/scheduler.py` `_build_tasks()` (mirror data.purge block 109–118, gated by flag) AND add `app.tasks.langgraph_prune` to the worker command module list in `ops/docker/docker-compose.yml` (145–152).
9. Document new env vars in `docs/reference/environment-variables.md` and reconcile the pool-size figure in `docs/vector-index-sync.md` connection-budget table.

**Files:** `app/db/models/core.py`, `app/config/{settings,__init__}.py`, `bot.py`, `app/api/main.py`, `app/tasks/scheduler.py`, `ops/docker/docker-compose.yml`, `docs/vector-index-sync.md`, `docs/reference/environment-variables.md`, `pyproject.toml`, `uv.lock`.
**New artifacts:** migration `0036`; `app/config/langgraph.py`; `app/infrastructure/checkpointing/runtime.py`; `app/tasks/langgraph_prune.py`; Postgres `langgraph` schema (4 tables via `.setup()`, NOT Alembic-managed); `tests/tasks/test_langgraph_prune.py` + lifespan/setup test; scheduler entry; new `LANGGRAPH_*` env vars.

**Risks.** `core.py` enum is multi-track hot — sequence/own it. Hard dep on T1 (`AsyncPostgresSaver`/`AsyncConnectionPool` absent from lock today; banned-api still blocks the import until T1 narrows it). Two drivers (asyncpg + psycopg3) + a non-Alembic schema — ops must know these tables are `.setup()`-managed; verify Alembic `env.py` `target_metadata` never tries to autogenerate them. `ALTER TYPE ADD VALUE` transaction behavior — verify `env.py` matches the 0027 precedent. Checkpoint blobs may carry PII — minimal-state contract is enforced in T5; prune is the backstop. Forgetting the compose module list means the task is enqueued but never consumed.

**Verification gates.** `make type`/`make lint` green (banned-api allows the saver import post-T1). Migration round-trips; `SELECT unnest(enum_range(NULL::llm_attempt_trigger))` includes `graph_node`. With flag on, lifespan creates the schema + 4 tables; with flag off, no pool/schema. Pool-isolation test (distinct psycopg3 pool, `Database` never used for checkpoint I/O). Prune test (aged rows deleted, Redis lock guards concurrency, early-return off). Connection-budget doc parity. Teardown closes the pool in both entrypoints. `thread_id == correlation_id` at the seam.

---

### T3 — Ports-and-Adapters Foundation

**Key:** `T3-ports-foundation` · **ADRs:** 0014, 0010, 0015, 0016, 0017, 0018 · **Effort:** M · **depends_on:** none · **parallel-safe:** ❌ (shared `.importlinter`, `ports/__init__.py`, `di/types.py`, `pyproject.toml`)

**Summary.** Establish the project-wide ports base for the rewrite: scaffold the three new application ports (retrieval, extraction, stream_sink), create `app/di/graphs.py` as the graph composition seam, and strengthen `.importlinter` toward the ADR-0014 layered contract. Lands the seams and the executable architecture definition without changing runtime behavior.

**Work items (ordered).**

1. `app/application/ports/retrieval.py`: `@runtime_checkable RetrievalPort` (ADR-0016) — async `retrieve(query|vector, entity_type, scope, top_k, filters)` and `find_similar(entity_type, id, top_k)`; `EntityType` discriminator; result DTO under `app/application/dto/` (extend `VectorSearchHitDTO`). Centralize the mandatory scope-filter contract in the docstring. Follow `search.py` conventions (`from __future__ import annotations`, TYPE_CHECKING only, no concrete deps).
2. `app/application/ports/extraction.py`: `@runtime_checkable ExtractionPort` (ADR-0015) — single async `extract(request) -> result`; reference (don't duplicate) `scraper/protocol.py::ContentScraperProtocol` and `platform_extraction/protocol.py::PlatformExtractor`.
3. `app/application/ports/stream_sink.py`: `@runtime_checkable StreamSinkPort` (ADR-0017) — `publish(request_id, event)`; framework-agnostic (no langgraph/astream_events types).
4. Register all three in `app/application/ports/__init__.py` `__all__` (alphabetical), import-safe.
5. `app/di/graphs.py` stub (ADR-0010): a `build_*` that wires port-typed node deps from concrete adapters/infrastructure; returns a new deps dataclass added to `app/di/types.py` (do NOT retro-type existing `Any` fields). No `StateGraph` yet.
6. Strengthen `.importlinter` (strangler-fig): add a layered contract treating `{adapters, infrastructure}` as ONE combined tier first (tolerates the existing 29 bidirectional imports). Tiers: `domain | application | {adapters, infrastructure} | {api, di, tasks}`. Land ONLY sub-contracts that pass today; document deferred ones as comments tied to the owning track.
7. Add a forbidden-contract scaffold (commented/disabled) for "adapters/api/tasks reach application via ports/use_cases, not concrete `app.application.services` classes" — the 58 violators migrate per owning track; enable at that cutover.
8. Flag the langgraph-ban cross-track prerequisite: `di/graphs.py` and the orchestration track cannot import langgraph until T1 lifts the ban. Do NOT lift it in T3.

**Files:** `.importlinter`, `app/application/ports/__init__.py`, `app/di/types.py`, `pyproject.toml`.
**New artifacts:** `app/application/ports/{retrieval,extraction,stream_sink}.py`; `app/di/graphs.py`; new `[importlinter:contract:*]` layered section; optional new DTO module / extension of `dto/vector_search.py`.

**Risks.** A strict adapters/infrastructure split FAILS today (3 infra→adapters + 26 adapters→infra files) — model as one tier first, tighten after sibling tracks remove cross-imports. Shared seams collide with T4/T5/T7/T8 (serialize). banned-api still forbids langgraph — keep `di/graphs.py` langgraph-free in T3. import-linter imports `app.*` at CI time — keep ports dependency-free (TYPE_CHECKING only) or break the job. 58 concrete-service imports mean the "via ports" forbidden cannot be enabled here. Don't retro-type existing `Any` DI fields.

**Verification gates.** `make lint && make type && make format` pass. `lint-imports` GREEN with the new layered contract; no regression of the 5 existing contracts. `python -c 'import app.application.ports.{retrieval,extraction,stream_sink}, app.di.graphs'` succeeds with no optional-dep errors. Each Protocol is `@runtime_checkable` and structurally matched by its intended adapter in a unit test. No prompt/model/Database/correlation changes. Ports exported via `__all__`.

---

### T4 — Unified Retrieval

**Key:** `T4-retrieval-unification` · **ADRs:** 0016, 0012, 0005, 0010, 0014, 0018 · **Effort:** XL · **depends_on:** T3 · **parallel-safe:** ❌

**Summary.** Implement `RetrievalPort` (retrieve + find_similar, entity_type-discriminated) backed by ONE Qdrant adapter that centralizes the mandatory environment+user_scope filter, rerank, and query expansion, then converge all current vector-search paths onto it as thin callers while keeping OpenAPI/MCP contracts byte-stable behind parity tests. **Scope is larger than ADR-0016 states: FIVE re-implementations converge, not three** — `StoreVectorSearchService`, `RepositorySearchService`, `GitMirrorSearchService`, MCP `SemanticSearchService`, and the legacy in-Postgres `VectorSearchService`.

**Work items (ordered).**

1. Define `RetrievalPort` + neutral DTOs (`RetrievalHit/Result/Scope`) generalizing `dto/vector_search.py`. `entity_type` ∈ `summary | repository | git_mirror | x_wiki` (match Qdrant payloads in `infrastructure/vector/point_ids.py`).
2. Implement ONE adapter `app/infrastructure/retrieval/qdrant_retrieval_adapter.py` (new package) reusing `QdrantVectorStore` + `EmbeddingProviderPort`. **Centralize the mandatory scope filter** (environment + user_scope always; per-entity user_id when user-scoped) so no caller can omit it — replacing the three divergent filter-build sites (`qdrant_store.query` 473–482; `RepositorySearchService` 143–175; `GitMirrorSearchService` 100–108 reaching into private `_client/_collection_name`).
3. Per-entity hydration inside the adapter (summary→Summary+Request, repository→Repository row, git_mirror→GitMirror row), preserving the defense-in-depth Postgres-side user_id re-filter.
4. Centralize rerank: fold `OpenRouterRerankingService` + local cross-encoder `RerankingService` + MCP lexical-overlap 0.82/0.18 blend behind one optional step; `rerank=False` reproduces current ordering exactly.
5. Centralize query expansion via the port's `expand_query` flag (route `QueryExpansionService`; replace MCP `_extract_query_tags`).
6. Add `find_similar` primitive: add `QdrantVectorStore.recommend/find_similar_by_id` (by-id recommend with mandatory scope filter — does NOT exist today) and migrate MCP `find_similar_articles` (601–651, currently re-embeds seed text) onto it; keep tool response shape byte-stable.
7. Migrate `StoreVectorSearchService` consumers (`api/services/search_service.py`, `di/search.py:117–123`, `di/mcp.py:115–119`) onto the port; reduce/delete the service.
8. Migrate `RepositorySearchService` (`/search/repositories` `api/routers/content/search.py:194–293`) — preserve `RepositorySearchResponse` `distance = 1 - similarity`.
9. Migrate `GitMirrorSearchService` (`/v1/git-mirrors/search` `api/routers/git_mirrors.py:252–275`) — preserve `GitMirrorSearchResponse`.
10. Migrate MCP `SemanticSearchService` (semantic/hybrid/find_similar). **Decision (record it):** in-Postgres cosine fallback `_search_local_vectors` (245–346) + keyword fallback live ABOVE the port (port returns empty, MCP keeps fallback orchestration). Keep all MCP JSON keys stable.
11. **Decide the legacy in-Postgres `VectorSearchService` fate** (40–276; used by `di/telegram.py:678`, `cli/search.py:95,147`): (a) back with port `entity_type='summary'` or (b) leave out of scope. Record; if left, it does NOT count as converged.
12. Compose in `app/di/retrieval.py` (or extend `di/search.py`): single adapter injected into API search service, MCP context, git_mirrors router. This is the seam ADR-0010 needs for the future `ground` node — retrieval logic belongs in the port adapter, not in the vector-sync/reconciler path.
13. Parity/contract tests (ADR-0018 gate, build BEFORE cutover): golden response-byte tests for `/search/semantic`, `/search/repositories`, `/v1/git-mirrors/search`, MCP `semantic_search`/`find_similar_articles`; `make check-openapi-drift` clean; scope-filter invariant test per entity_type.
14. Add the import-linter port-only contract LAST (forbid api/adapters/mcp importing concrete `infrastructure.search.*`/`infrastructure.retrieval.*`); land green at cutover; delete dead modules.

**Files:** `app/application/ports/search.py`, `app/application/dto/vector_search.py`, `app/infrastructure/search/*` (vector/repository/git_mirror/hybrid/reranking/query_expansion/filters), `app/infrastructure/vector/{qdrant_store,protocol,result_types}.py`, `app/mcp/{semantic_service,context}.py`, `app/api/routers/content/search.py`, `app/api/routers/git_mirrors.py`, `app/api/services/search_service.py`, `app/di/{search,mcp,telegram}.py`, `app/cli/search.py`, `.importlinter`.
**New artifacts:** `app/application/ports/retrieval.py`; `app/infrastructure/retrieval/{__init__,qdrant_retrieval_adapter}.py`; `app/di/retrieval.py`; `QdrantVectorStore.find_similar_by_id`; new import-linter port-only contract; `tests/test_retrieval_port.py` + parity tests; ADR-0016 status flip.

**Risks.** ADR-0016 undercounts duplication (FIVE, not three). Repo/mirror services bypass `query()` and hand-build native filters (MatchAny/MinShould) reaching into privates — the adapter must absorb both without regressing semantics. Scope-filter centralization is IDOR-critical (ADR-0005/0012, rule 12). MCP carries degrade-path logic others lack. distance-vs-similarity conventions differ (1-score, 1-similarity, 4dp rounding) — subtle numeric drift breaks parity. `find_similar` is a fresh correctness surface. No existing parity tests — the net must be built before any cutover.

**Verification gates.** `make lint && make type && lint-imports` green incl. the port-only contract. `make check-openapi-drift && check-openapi-validate` ZERO change for `/search*` + `/v1/git-mirrors/search`. Parity tests byte/JSON-equal per endpoint and MCP tool at default flags. Scope-invariant test proves environment+user_scope(+user_id) on every path. `find_similar` excludes seed id. Coverage ≥ 80%. Post-cutover: converged modules deleted (or legacy `VectorSearchService` retained with recorded reason); no transitional flag outlives the migration.

---

### T5 — Graph Skeleton + Runtime Contract

**Key:** `T5-graph-skeleton` · **ADRs:** 0010, 0011, 0001, 0004, 0013, 0015, 0018 · **Effort:** L · **depends_on:** T3, T2 · **parallel-safe:** ❌ (owns `nodes/*` + `graph.py` + edits `attributes.py`)

**Summary.** Build the greenfield LangGraph summarize `StateGraph` skeleton under `app/application/graphs/summarize/` — a serializable id-based `SummarizeState` TypedDict, async node stubs calling only application ports (deps via config/`functools.partial`, never in state), `thread_id=correlation_id`, per-node OTel spans reusing `app/observability/otel.py`, failure mapping to the existing `RequestProcessingJob`/`RequestStatus.ERROR` lifecycle, and a compile entrypoint taking an in-memory checkpointer first (swappable to T2's `AsyncPostgresSaver`).

**Work items (ordered).**

1. `app/application/graphs/{__init__,summarize/__init__}.py` (new package).
2. `app/application/graphs/summarize/state.py`: `SummarizeState` TypedDict, serializable primitives ONLY (ADR-0011) — `correlation_id:str`, `request_id:int`, `lang:str`, `grounding_ids:list[str]`, `summary:dict`, `validation_errors:list[str]`, `repair_attempts:int`, `call_count:int`. NO live deps; content re-fetched by `request_id`.
3. `app/application/graphs/summarize/deps.py`: deps bundle typed ONLY against application ports (llm_client, summaries, requests, T3 retrieval). MUST NOT import `app.adapters.*`/`app.infrastructure.*` (preserves `application-no-outward`, which already auto-covers `app.application.graphs`).
4. `nodes/` stubs as plain `async def node(state, *, deps) -> dict` for the ADR-0015 graph (ingest, extract, ground, build_prompt, summarize, validate, repair, enrich, persist, notify). T5 ships signatures + wiring; bodies land in sibling tracks. No langgraph import inside node bodies.
5. `graph.py`: the SINGLE langgraph-coupled surface (`StateGraph`, add_node, add_edge / add_conditional_edges for validate→repair↺validate + error→lifecycle, compile).
6. Per-node OTel spans reusing `otel.get_tracer` + `start_as_current_span` + `set_correlation_id_attr` + `attributes.py` constants; add new `ratatoskr.graph.*` constants to `attributes.py` (match `url_processor.py:227–230` pattern).
7. `lifecycle.py` (or in graph.py): node exception, `GraphRecursionError`, call-budget exhaustion ALL route to the existing terminal path — `RequestProcessingJob` terminal + `RequestStatus.ERROR` + `Error ID: <correlation_id>`; reuse `failure_observability.persist_request_failure`. NO parallel error path. Reference `url_processor.py:430–490`.
8. Compile entrypoint with a pluggable checkpointer arg: T5 uses `InMemorySaver` (testable without T2 pool); T2's `AsyncPostgresSaver` injected at the same seam later. `thread_id=correlation_id` + `recursion_limit` PER-INVOCATION in config.
9. `app/di/graphs.py` (per ADR-0010): wire concrete adapters into port-typed node deps + supply checkpointer (langgraph import allowed in di).
10. Thin graph-invocation use case/runner behind `SUMMARIZE_GRAPH_ENABLED` (removed at cutover).
11. Unit tests: state msgpack-serializable, graph compiles with InMemorySaver, `thread_id=correlation_id` propagates, node exception → ERROR + Error ID (mock lifecycle), import-linter green. Tests under `patch.dict(...clear=True)` inject `MODEL_SELECTION_ENV`.

**Files (read-only refs):** `pure_summary_service.py`, `interactive_summary_service.py`, `url_processor.py`, `platform_extraction/lifecycle.py`, `observability/otel.py`, `di/application.py`, `.importlinter`, `tests/_config_env.py`. **Edited:** `app/observability/attributes.py`, `app/db/models/core.py` (read-only; enum from T2).
**New artifacts:** `app/application/graphs/__init__.py`, `.../summarize/{__init__,state,deps,graph,lifecycle}.py`, `.../summarize/nodes/__init__.py` + per-node stubs, `app/di/graphs.py`, new `ratatoskr.graph.*` attributes, skeleton tests.

**Risks.** Hard deps on T1 (langgraph ban/extra), T2 (`graph_node` enum + AsyncPostgresSaver), T3 (`retrieval.py` shape) — coordinate deps signature with T3 before finalizing. NOT parallel-safe with sibling node-body tracks (T5 owns `nodes/*` + `graph.py`; they layer on after). `attributes.py` collides with any observability track. State must be strictly msgpack-serializable — a Pydantic/dataclass/port field breaks checkpointing at runtime, not type-check; the serialization test is the only guard. Failure-mapping must hit the EXACT legacy terminal contract — the parity test is the gate. `thread_id=correlation_id` is sacred — no node mutates it.

**Verification gates.** `make lint` green (banned-api allows langgraph/langchain_core post-T1). `lint-imports` green: graphs import zero adapters/api. `make type` green. State round-trips msgpack. Graph compiles with InMemorySaver; `thread_id`/`recursion_limit` per-invocation. Node exception + simulated `GraphRecursionError` + budget exhaustion → ERROR + Error ID (single helper). Each node opens an OTel span carrying `ratatoskr.correlation_id`. Deps injected via config/partial, never in checkpointed state. `MODEL_SELECTION_ENV` injected in clear-env tests. Independently-green PR; `SUMMARIZE_GRAPH_ENABLED` gates the path so default behavior unchanged.

---

### T6 — RAG `ground` Node + Read-Your-Writes Freshness

**Key:** `T6-rag-node-freshness` · **ADRs:** 0005, 0012, 0010, 0015, 0016, 0018 · **Effort:** L · **depends_on:** T4, T5 · **parallel-safe:** ❌

**Summary.** Add a `ground` node using the T4 retrieval port to fetch top-k scope-filtered prior summaries and inject them as a clearly-delimited "related prior summaries (reference only)" block into the summarize system prompt, plus a synchronous read-your-writes fast-path that upserts each newly-created summary into Qdrant on the persist path before the request is marked done — the reconciler handles backfill/repair only.

**Work items (ordered).**

1. **GAP (resolved in T9 cutover):** the persist node now writes a read-your-writes Qdrant point synchronously via `app/infrastructure/vector/summary_point.py`; summaries are immediately retrievable. The Taskiq reconciler handles convergence/backfill (ADR-0012).
2. Config (rule 11 / ADR-0018): add `SUMMARIZE_RAG_ENABLED` (default OFF) + `RAG_TOP_K` (default 5) in `app/config/`; record explicit removal trigger. Embedding models stay from `ratatoskr.yaml` — no code defaults.
3. `nodes/ground.py` as plain `async def ground(state, deps) -> dict`: no-op when flag off OR retrieval unavailable; else embed article text and call `port.retrieve(query=..., entity_type='summary', scope=<user_scope/environment>, top_k=RAG_TOP_K)`. Depends ONLY on the application retrieval port.
4. Enforce mandatory scope filter (ADR-0005/0012, rule 12): pass user_scope/environment; **exclude the current request_id**; never bypass the centralized filter.
5. Anti-contamination block: format hits (title + tldr/summary_250 snippet, no raw source) under an explicit delimiter ("related prior summaries — reference only; do NOT summarize these, do NOT introduce facts/cross-references absent from the source"). Update all 4 prompt files in lockstep (rule 7): `summary_system_{en,ru}.txt` + `summary_system_{en,ru}_instructor.txt`.
6. Wire into prompt assembly via the `build_prompt` node (T7 owns it): ground writes the block into graph state, build_prompt concatenates. **Coordinate the shared state key + build_prompt edit with T7** — do not edit build_prompt independently. (Legacy seam for reference: `summary_request_factory.py` ~281/339/361/370, `pure_summary_service.py` ~110–117.)
7. Synchronous index-on-write in the persist node: after finalize, build embedding text via `core/embedding_text.prepare_text_for_embedding`, embed, build payload reusing `vector/metadata_builder.MetadataBuilder` + `note_text_builder.build_note_text` + `point_ids.summary_point_id`, call `vector_store.replace_request_notes(request_id, ...)` BEFORE marking done.
8. Best-effort/non-blocking (ADR-0012): try/except; on Qdrant failure log (with correlation_id) + leave to reconciler; summary persists, request completes. Run blocking qdrant-client via `asyncio.to_thread`.
9. Fast-path = freshness; reconciler = convergence/backfill (doc/comment only). **Verify payload byte-compat** with `app/infrastructure/vector/summary_point.py` (entity_type/summary_id/request_id/user_scope/environment/language/...) so `query()` and the reconciler see no drift/duplicate.
10. Route through the SAME T4 unified port (ADR-0016) — do NOT reintroduce a parallel path. (`related_reads_service.py` is the post-summary notification analogue via `VectorSearchPort`; ground is the pre-summarize grounding analogue and must use the unified port.)
11. Tests: flag-OFF parity (grounded == legacy); ground no-op paths; scope-filter (no cross-scope leakage, current request excluded); read-your-writes integration (create A, summarize B, ground retrieves A immediately, no poll); upsert-failure resilience.

**Files:** `nodes/ground.py`, `summary_system_{en,ru}{,_instructor}.txt`, `summary_request_factory.py`, `pure_summary_service.py`, `url_summary_delivery_service.py`, `application/services/summarization/llm_response_workflow_attempts.py`, `app/config/`, `app/di/graphs.py`, `docs/reference/environment-variables.md`, `CLAUDE.md`.
**New artifacts:** `nodes/ground.py`; `SUMMARIZE_RAG_ENABLED` + `RAG_TOP_K`; synchronous index-on-write helper (reusing MetadataBuilder + summary_point_id + replace_request_notes); anti-contamination formatter; the 5 tests above.

**Risks.** Hard dep on T4 port + T5 scaffold + T7 build_prompt — coordinate or stub against agreed signatures. File-edit collisions with T7 (build_prompt, persist, the 4 prompt files). Payload drift vs `_build_summary_payload` → false reconciler drift / `query()` mismatch (reuse same builder + point_id namespace). Latency/availability — best-effort, `asyncio.to_thread`. Anti-contamination is partly prompt, partly validator (ADR-0005) — verify the validate node guards injected cross-references or scope as follow-up. Grounded output is non-deterministic — only flag-OFF parity is assertable. Avoid double-embedding (ground query vs persist summary); per ADR-0005 retrieval embedding does NOT count against `llm_max_calls_per_request`.

**Verification gates.** `lint-imports` green (`application-no-outward`: ground imports only the retrieval port). Flag-off parity byte-identical. Read-your-writes proof (ground retrieves A with no poll). Scope-safety proof (cross-scope/other-user never returned; current request excluded). Resilience proof (Qdrant raises → summary persists, request COMPLETED, warning with correlation_id). Payload-compat proof (same `summary_point_id`, reconciler reports no drift). en+ru lockstep (delimiter present in both). `make lint/type/pytest`, coverage ≥ 80%, `make check-openapi` (no API change), B006/B023 not suppressed.

---

### T7 — Extraction Port + Pipeline Collapse

**Key:** `T7-extraction-pipeline-collapse` · **ADRs:** 0015, 0010, 0011, 0013, 0014, 0017, 0018 · **Effort:** XL · **depends_on:** T5, T3 · **parallel-safe:** ❌ (largest blast radius)

**Summary.** Introduce an `extraction` port whose single adapter dispatches by source kind to the scraper-chain algorithm (kept whole inside its adapter) OR the youtube/twitter/academic/github/meta platform extractors, then collapse the url_processor / interactive_summary_service / pure_summary_service / ContentExtractor indirection into graph nodes (ingest/extract/ground/build_prompt/summarize/validate/repair/enrich/persist/notify). Largest track: owns the `extract` node + the whole pipeline collapse, gated by an all-source-kind parity test before legacy deletion.

**Work items (ordered).**

1. **Audit reality:** dispatch is NOT a `requests.source_kind` column (none exists; `Request` has only `route_version`, `core.py:322`). Real dispatch is URL-pattern predicates in `di/platform_extractors.py::build_platform_extractor_descriptors` (is_github/academic/youtube/twitter/threads/instagram) routed by `PlatformExtractionRouter`, with `ContentScraperChain.scrape_markdown` as fallback. The port wraps THIS predicate-router + chain fusion (today fused inside `ContentExtractor.extract_and_process_content`). **Source kinds are broader than CLAUDE rule 8: also github + meta(threads/instagram).**
2. Define `app/application/ports/extraction.py`: `ExtractionPort` with `async extract(request: ExtractionRequest) -> ExtractionResult` + serializable DTOs mirroring `PlatformExtractionResult` fields (content_text, content_source, detected_lang, title, images, metadata, request_id, source_item, normalized_document). **No Telegram `message` objects** in DTOs — notification concerns go through the notify node.
3. Implement `app/adapters/content/extraction_adapter.py` (or slim `ContentExtractor`): (a) normalize URL via `url_utils.normalize_url` + sha256 dedupe key, (b) `PlatformExtractionRouter.extract` first, (c) fall back to `ContentScraperChain.scrape_markdown` inside `self._sem()`, (d) `detect_low_value_content` + `persist_request_failure` on failure (preserve `crawl_results` + `REASON_FIRECRAWL_*` codes), (e) build `NormalizedSourceDocument`. **Chain stays a cohesive algorithm — do NOT explode rungs into nodes** (ADR-0015).
4. Wire in DI (`app/di/extraction.py` or extend `di/shared.py`): compose from `build_registered_platform_router(...)` + chain; nodes receive the port via graph config/partial.
5. `nodes/extract.py` `async def extract(state, deps) -> dict`: store only request_id, dedupe_hash, content_source, detected_lang, title, image-handle ids (minimal state); re-fetch content_text by request_id downstream. Map failure to the existing terminal path; NO parallel error path.
6. Collapse `pure_summary_service` into summarize/build_prompt: `select_max_tokens` (1536/12288), long-context routing (`long_context_threshold_tokens` + `long_context_model` + token-aware truncation), content-aware routing (`classify_content`/`resolve_model_for_content`), `clean_content_for_llm` + `build_summary_user_prompt`, sticky-failure force-fallback (`_classify_sticky_error`: per_model_timeout/repeated_truncation/truncation_recovery_skipped_budget_tight + override-drop retry), `_load_instructor_prompt` (en+ru lockstep) — all via `llm_client.chat_structured` (ADR-0006).
7. Collapse two-pass enrichment (`enrich_two_pass` + `enrichment_system_{en,ru}.txt`) into the optional `enrich` node, gated by `cfg.runtime.summary_two_pass_enabled`.
8. Collapse `interactive_summary_service` into validate/repair/persist/notify: bind the existing repair loop (`llm_response_workflow*.py`) to validate→repair↺validate bounded by repair_attempts + recursion_limit; move cache lookup/write (`LLMSummaryCache`), insights reset/update, `ensure_summary_metadata`, `mark_prompt_injection_metadata`, `merge_summary_quality_metadata`, empty-content path into nodes. Notifications (start/completion/error, progress/typing) → notify node.
9. Collapse `url_processor.handle_url_flow`/`_run_url_flow_inner`: cached short-circuit (`CachedSummaryResponder`) → ingest guard; `URLFlowContextBuilder` + chunking → ingest/extract; stage publishes → `stream_sink` port (ADR-0017, T8) from summarize node, NOT inline; `RequestProcessingJobRepository.record_synchronous_start/outcome` (crash-recovery lease) + per-request latency metric → graph entry/finally; `post_summary_tasks.schedule_tasks` → after persist/notify.
10. Persist-everything inside the graph: persist node writes `llm_calls` (incl. failures) with `attempt_index` + `attempt_trigger='graph_node'` (coordinate with T2 enum); summaries + telegram_messages preserved; correlation_id on every row.
11. Parity gate (ADR-0013/0018): golden/fixture test proving graph ≡ legacy across ALL source kinds — web_article, youtube_video, x_post/x_article, academic_paper, **github_repository, threads/instagram (meta)**, forwarded — AND budget / sticky-fallback / two-pass / chunking. Green per source_kind before flipping default.
12. Migrate callers at cutover (delete-in-one-step): `cli/summary.py:188`, `cli/retry.py:109`, `tasks/url_processing.py`, `telegram/url_handler.py`, `agents/multi_source_extraction_agent.py`, `ingestors/x_bookmarks_ingestor.py`, `rss/rss_delivery_service.py`, `content/forward_link_enricher.py`, `api/background/handlers.py`; DI sites `di/{shared,tasks,telegram_commands}.py`.
13. Delete legacy at cutover: `url_processor.py`, `interactive_summary_service.py`, `pure_summary_service.py`, `summary_request_factory.py`, `summarization_runtime.py`, `content_extractor{,_crawl,_requests}.py`, `cached_summary_responder.py`, `url_flow_*` glue — **after grep-verifying nothing reusable (budget/semaphore/sticky/two-pass/persist) remains un-migrated.** Remove `SUMMARIZE_GRAPH_ENABLED`.
14. Add import-linter `extraction` contract (forbid concrete extraction-adapter imports outside DI); keep `content-no-telegram` + `application-no-outward` green (extract DTOs carry no Telegram objects).

**Files (selected):** `url_processor.py`, `pure_summary_service.py`, `interactive_summary_service.py`, `content_extractor{,_crawl,_requests}.py`, `summarization_runtime.py`, `summary_request_factory.py`, `content_chunker.py`, `cached_summary_responder.py`, `url_flow_*`, `platform_extraction/{router,registry,protocol,models,lifecycle}.py`, `scraper/{chain,protocol}.py`, `{youtube,twitter,academic}/platform_extractor.py`, `streaming/{stream_hub,events}.py`, `di/{platform_extractors,shared,tasks,telegram_commands}.py`, `cli/{summary,retry}.py`, `tasks/url_processing.py`, `telegram/url_handler.py`, `.importlinter`.
**New artifacts:** `app/application/ports/extraction.py`; `app/adapters/content/extraction_adapter.py`; `app/di/extraction.py`; `nodes/extract.py` + `{ingest,build_prompt,summarize,validate,repair,enrich,persist,notify}.py` bodies; `tests/.../test_extract_node_parity.py` (all-source-kind fixtures); extraction-port-boundary contract.

**Risks.** ADR-0015 says dispatch by `source_kind` but no column exists — wrap the predicate-router or mis-route silently. Source kinds broader than CLAUDE/ADR enumerate — parity MUST cover github + meta. Largest blast radius (~6 services + ContentExtractor, 3000+ lines) — high chance of dropping load-bearing behavior. Overlaps `nodes/*` + `di/*` with T5/T6/T8 — sequence (T7 first within Wave 3). Persist-everything easy to drop when collapsing mixins. Hard cutover = forward-fix only — the parity test carries ALL safety. Stage events must move behind `stream_sink` without breaking SSE/Telegram drafts. GitHub/meta extractors pull heavy deps — preserve lazy construction.

**Verification gates.** `make lint && make type` green (ruff ≥ 0.15.13; B006/B023 never suppressed). `lint-imports` green incl. extraction-port-boundary + `application-no-outward` + `content-no-telegram`. Parity green for EVERY source_kind. Behavior-parity assertions (budget, long-context truncation, sticky force-fallback, two-pass, chunking, Redis cache). DB assertions (`crawl_results` + `llm_calls` incl. failures with `attempt_index`/`attempt_trigger`/correlation_id; summaries/telegram_messages unchanged). Failure path → `RequestProcessingJob` ERROR + Error ID, no parallel path. Per-node unit tests as plain `async def(state, deps)`. Post-cutover CLI smoke per source_kind matches pre-cutover golden; legacy deleted; `SUMMARIZE_GRAPH_ENABLED` removed.

---

### T8 — Streaming Under the Graph

**Key:** `T8-streaming` · **ADRs:** 0017, 0010, 0011, 0014, 0015, 0013, 0018 · **Effort:** M · **depends_on:** T5 · **parallel-safe:** ❌

**Summary.** Bridge LangGraph `astream_events` from the `summarize` node into the existing in-process `StreamHub` via the new `stream_sink` port, keeping streamed tokens as an ephemeral side-channel (never in checkpoint state). SSE and Telegram-draft consumers stay byte-for-byte unchanged; only the producer side moves from the legacy callback chain to the graph.

**Work items (ordered).**

1. `app/application/ports/stream_sink.py` `StreamSink` port (T3-scaffolded; finalize surface): async `stage(stage)`, `section(section, content, partial)`, `warning(code, message)`, `done(summary_id, request_id)`, `error(code, message, correlation_id)`. Carries `request_id` + `correlation_id` per call; never imports `StreamHub`. Register in `ports/__init__.py`.
2. `app/adapters/content/streaming/stream_sink_hub.py` `StreamHubStreamSink`: wrap `get_stream_hub()`/injected `StreamHub` + `StreamEvent.now(...)`. The SINGLE streaming-coupled surface; reuse exact `Stage/Section/Warning/Done/ErrorPayload` shapes from `streaming/events.py` so consumers need zero changes.
3. `app/adapters/content/streaming/graph_event_bridge.py`: async helper consuming `graph.astream_events(...)` → StreamSink calls — token deltas → reuse `SummarySectionStreamAssembler.add_delta()` for section snapshots; node transitions → `stage` events (extracting/summarizing/validating/persisting/done) matching `ProcessingStage`. Keep the assembler OUTSIDE checkpoint state (per-invocation, ephemeral). Replaces inline `get_stream_hub().publish('stage', ...)` in `url_processor.py:294/371/375/391` + `url_flow_context_builder.py:112`.
4. Token feed decision (document in bridge docstring): ADR-0017 mandates `astream_events` as producer; drive node logic off astream_events token events but reuse the assembler for section extraction (feed it the model content stream, not LangChain message-wrapper events).
5. Wire composition in `app/di/graphs.py`: inject `StreamHubStreamSink` into summarize-node deps as a port-typed arg; run `graph_event_bridge` concurrently with `graph.ainvoke/astream`; bridge lifecycle (create on run start, cancel/await on terminal done/error) lives in the runner, NOT the node.
6. Migrate legacy producer sites under cutover (coordinate with T7): remove inline `publish('stage'/'section')` from `url_processor.py` (~294–393) + `url_flow_context_builder.py` (~109–115); re-point `summary_request_factory.py:_configure_streaming` (678 on_stream_delta) + `di/shared.py:225` (stream_coordinator_factory) at the graph path. Telegram draft rendering (`SummaryDraftStreamCoordinator`) survives unchanged (consumer-side).
7. Terminal/lifecycle alignment: `api/background/progress.py::BackgroundProgressPublisher._publish_local` (83–114) already mirrors done/error into the hub from the durable progress repo. Ensure the runner's done/error does NOT double-emit — each request_id stream terminates on exactly one done/error (`stream_hub.py:24` treats them terminal).
8. Tests: (1) `StreamHubStreamSink` emits StreamEvents structurally identical to today for every kind; (2) bridge translates a fake astream_events sequence into expected ordered StreamSink calls + section snapshots; (3) resume/replay test proving a half-stream is NOT replayed from a checkpoint (re-run re-streams). Reuse `tests/integration/api/test_request_stream.py` + `tests/integration/telegram/test_url_flow_streaming.py` as parity oracles.
9. Docs: `streaming/__init__.py` docstring + CLAUDE.md streaming refs name the stream_sink port + graph_event_bridge as the new producer; StreamHub remains the pub/sub surface.

**Files:** `streaming/{stream_hub,events,section_assembler,__init__}.py`, `telegram/summary_draft_streaming.py`, `url_processor.py`, `url_flow_context_builder.py`, `summary_request_factory.py`, `telegram/forward_summarizer.py`, `api/routers/content/streams.py`, `api/background/progress.py`, `di/shared.py`, `application/ports/__init__.py`, `application/dto/stream_enums.py`.
**New artifacts:** `app/application/ports/stream_sink.py`; `streaming/stream_sink_hub.py`; `streaming/graph_event_bridge.py`; `tests/unit/streaming/test_stream_sink_hub.py`, `test_graph_event_bridge.py`.

**Risks.** Hard dep on T5 (summarize node, SummarizeState, di/graphs.py, astream_events runner). Edits to url_processor/url_flow_context_builder/summary_request_factory/di/shared overlap with T5/T7 — sequence after T7 owns those files (or single coordinated change). `application-no-outward` forbids the port importing StreamHub — adapter injected via DI. Double-emit of terminal done/error (runner vs BackgroundProgressPublisher) could truncate the stream early. Checkpoint contamination — assembler buffer/partial tokens must never enter SummarizeState. Token-feed granularity mismatch (astream_events vs OpenRouter SSE deltas) — feed the assembler raw model JSON content, not message wrappers.

**Verification gates.** `lint-imports` green (no `app.application -> app.adapters.streaming` edge). Consumer parity: `test_request_stream.py` + `test_url_flow_streaming.py` pass UNCHANGED against the graph stream. New unit tests pass (event-shape parity, bridge ordering, resume-does-not-replay). CLI/SSE smoke: live section previews + stage transitions identical to legacy. Checkpoint dump mid-run has no stream buffer/token text. `make lint && make type` green; correlation_id on every emitted StreamEvent.

---

### T9 — Parity Net + Hard Cutover + Flag Retirement

**Key:** `T9-parity-cutover-flags` · **ADRs:** 0013, 0018, 0015, 0011, 0010, 0001 · **Effort:** XL · **depends_on:** T5, T6, T7, T8 · **parallel-safe:** ❌

**Summary.** The final code track: write a comprehensive golden/parity suite proving the LangGraph summarize graph (T5–T8) matches the legacy path across every source_kind plus budget/sticky-fallback/two-pass behaviors, then perform the hard cutover — flip the default, delete `pure_summary_service` + `url_processor`/`interactive_summary_service` indirection, retire all transitional flags, update docs/CLAUDE/skills. Gated entirely by the parity net; touches legacy files + ~30 app callers + ~20 test files.

**Work items (ordered).**

1. **GAP-ZERO prerequisite check:** confirm T5–T8 delivered the graph (graphs package, retrieval port, di/graphs.py, `graph_node` enum, langgraph un-ban, transitional flags). T9 cannot start otherwise.
2. **PARITY SUITE (write FIRST):** `tests/parity/test_summarize_graph_parity.py` (dir already holds `test_contract_consistency.py`). Assert graph ≡ legacy for the same fixture across ALL five ADR-0013 source_kinds (generic URL, YouTube, Twitter/X, academic, forwarded). Compare via `validate_and_shape_summary` (normalizes non-deterministic text); **mock the llm_client port** so both paths get identical canned responses.
3. PARITY — budget/token: golden-test `select_max_tokens` (1536/12288 dynamic budget) + long-context routing/truncation (`long_context_threshold_tokens`, chars_per_token) produce identical max_tokens/model_override in summarize/build_prompt nodes.
4. PARITY — sticky-failure: reproduce `_classify_sticky_error` + at-most-one override-drop retry (3 sticky classes, gated by `runtime.llm_sticky_failure_force_fallback`); port `tests/adapters/content/test_pure_summary_sticky_failure.py` onto the graph node.
5. PARITY — two-pass: golden-test `enrich_two_pass` (enrichment_{en,ru} prompts, 8-key merge, content_text[:30000] cap) gated by `runtime.summary_two_pass_enabled`, incl. en+ru lockstep.
6. PARITY — content-aware routing: assert same `display_model` as `url_processor._run_url_flow_inner` (262–275) / `pure_summary_service` (86–98) via `classify_content` + `resolve_model_for_content`.
7. PARITY — persistence/observability: assert `llm_calls` with `attempt_index` + `attempt_trigger='graph_node'`, summaries written, correlation_id as thread_id, same terminal `RequestProcessingJob` status + `Error ID: <correlation_id>` on failure (parity vs `url_processor.py:410–491`).
8. PARITY — cached/chunked: cover chunking branch (`url_processor.py:301–335`, `content_chunker.process_chunks` + `semantic_helper.enrich_with_rag_fields`) and the Redis summary-cache hit/write (`interactive_summary_service.py:220–273`).
9. Get parity GREEN per source_kind behind `SUMMARIZE_GRAPH_ENABLED`, THEN flip default + delete legacy in one step (ADR-0013 sequence).
10. **HARD CUTOVER — flip default:** set `SUMMARIZE_GRAPH_ENABLED` true (or remove the branch). Re-point ~30 `URLProcessor.handle_url_flow` callers: `tasks/url_processing.py`, `di/{telegram,telegram_commands,types,shared,api}.py`, `cli/{retry,summary}.py`, `mcp/aggregation_service.py`, `ingestors/x_bookmarks_ingestor.py`, `telegram/{telegram_bot,message_handler,url_batch_processor,command_dispatcher,url_handler}.py`, `telegram/command_handlers/{url_commands_handler,admin_handler}.py`, `telegram/command_dispatch/state.py`, `api/background_processor.py`, `api/background/{handlers,db_override,executor}.py`, `api/routers/content/aggregation.py`.
11. Re-point non-indirection `PureSummaryService` callers: `di/tasks.py`, `core/escalation_policy.py`, `rss/rss_delivery_service.py`, `api/background/handlers.py` → call the graph (or a thin port/use-case) preserving their semantics.
12. **DELETE legacy:** `pure_summary_service.py`, `url_processor.py`, `interactive_summary_service.py`, and orphaned collaborators (summarization_runtime/summary_request_factory/url_flow_* glue) — grep-verify before deleting; confirm reusable logic already moved into nodes/ports.
13. **RETIRE transitional flags:** delete `SUMMARIZE_GRAPH_ENABLED` + dev-only parity toggles; remove `Field()`s from `app/config/runtime.py` + env entries. **Coordinate which flags die here vs their owning track** — `SUMMARIZE_RAG_ENABLED` (T6) and `LANGGRAPH_CHECKPOINT_ENABLED` (T2) retire with their migration, not prematurely. Keep load-bearing knobs (`summarization_max_retries`, `summary_two_pass_enabled`, `llm_sticky_failure_force_fallback`, `url_flow_streaming_enabled`).
14. MIGRATE/DELETE legacy-targeted tests: `tests/test_pure_summary_service.py`, `test_interactive_summary_service.py`, `adapters/content/test_pure_summary_service_helpers.py`, `test_pure_summary_sticky_failure.py`, `test_url_processor_batch_mode.py`, `test_url_processor_translation.py`, `test_redis_cache_layer.py`, `adapters/test_rss_delivery_service.py`, `api/test_background_processor.py`, + ~20 files importing URLProcessor. Re-home valuable assertions onto graph nodes/parity suite.
15. UPDATE docs/CLAUDE/skills (ADR-0018 DoD): rewrite `.claude/skills/langgraph-summarize-loop/SKILL.md` to point at the real graph (it currently describes a non-existent loop + omits `graph_node`); update CLAUDE.md Implementation Map rows naming the deleted files + Key File References; update `docs/explanation/{architecture-overview,multi-agent-architecture,design-philosophy,summary-contract-design}.md`, `docs/reference/troubleshooting.md`; flip affected ADR statuses to impl-complete.
16. FINAL GATE: `make lint && make type && make test` (80% floor) + import-linter; confirm langgraph/langchain_core confined to the graph-assembly module; confirm no dangling `SUMMARIZE_GRAPH_ENABLED` anywhere.

**Files (selected):** `pure_summary_service.py`, `url_processor.py`, `interactive_summary_service.py`, `config/runtime.py`, `db/models/core.py`, `pyproject.toml`, `CLAUDE.md`, `.claude/skills/langgraph-summarize-loop/SKILL.md`, `tests/parity/*`, the legacy-targeted tests, the ~30 caller modules, `docs/explanation/*`, `docs/reference/troubleshooting.md`.
**New artifacts:** `tests/parity/test_summarize_graph_parity.py` (graph ≡ legacy across 5 source_kinds + budget/sticky/two-pass/persistence/cache/chunk); `tests/parity/fixtures/` (canned extraction inputs + canned LLM responses, if not from T5–T8).

**Risks.** BLOCKED on prereqs (nothing to cut over to until T5–T8 land). Parity-net completeness IS the entire safety — a missed source_kind/behavior is a silent post-cutover regression with no fallback. Non-deterministic LLM output → mock the port + compare shaped dicts. Large deletion blast radius (~30 app + ~20 test files) — grep-verify every reference re-pointed before deleting. Flag-retirement scoping ambiguity — coordinate cross-track. Orphan-collaborator risk — some glue may still be used by nodes; per-file grep before removal. Invariant breakage during cutover — every invariant in §1 is an explicit reviewer gate.

**Verification gates.** Parity suite green: graph ≡ legacy (or ≡ frozen golden once legacy deleted) for generic-URL/YouTube/Twitter/academic/forwarded. Behavior parity proven (budget + long-context truncation, sticky 3-class, two-pass, content routing, persistence `attempt_trigger='graph_node'`, cache/chunk). `grep -rn 'SUMMARIZE_GRAPH_ENABLED' app/ tests/ config/ docs/` → nothing. `grep -rn 'pure_summary_service\|PureSummaryService\|class URLProcessor\|InteractiveSummaryService' app/` → nothing outside docs/history. `make lint && type && test` ≥ 80%. import-linter green incl. `application-no-outward`; langgraph confined to graph-assembly. Docs/CLAUDE/skills self-consistent (SKILL points at the real graph + lists `graph_node`). Manual CLI smoke per source_kind on the graph path.

---

## 4. Milestones & Exit Criteria

| Milestone | Wave | "Done" means |
|---|---|---|
| **M0 — Build can import the graph** | Wave 0 (T1) | `uv sync --extra graph` imports langgraph + AsyncPostgresSaver + psycopg_pool; banned-api allows langgraph/langchain_core, still blocks langchain/community; lockfile-drift CI green; default image unaffected. |
| **M0b — Seams exist** | Wave 0 (T3) | Three ports + `di/graphs.py` stub merged; new layered import-linter contract green; 5 existing contracts unregressed; ports import-light. |
| **M1 — Infra + retrieval ready** | Wave 1 (T2, T4) | Migration 0036 round-trips (`graph_node` in enum); checkpointer `.setup()` creates `langgraph` schema under flag; prune task registered + consumed; pool isolated from `Database`. Unified retrieval adapter live; all 5 vector paths converged (or legacy `VectorSearchService` retained with recorded reason); OpenAPI/MCP byte-stable; scope-invariant test green; port-only contract green. |
| **M2 — Graph skeleton compiles** | Wave 2 (T5) | Graph compiles with InMemorySaver; `SummarizeState` msgpack-serializable; `thread_id=correlation_id`; failure → single terminal helper; per-node OTel spans; node stubs + `di/graphs.py` wiring; behind `SUMMARIZE_GRAPH_ENABLED` (default off). |
| **M3 — Node bodies + streaming** | Wave 3 (T7→T6,T8) | All node bodies implemented; extraction port + adapter live; `ground` node + read-your-writes fast-path; streaming bridged via stream_sink; flag-OFF parity byte-identical; consumer streaming tests pass unchanged; persist-everything assertions green. |
| **M4 — Parity gate** (the gate before cutover) | Wave 4 (T9, pre-flip) | Parity suite GREEN per source_kind (incl. github + meta) AND for budget/sticky/two-pass/routing/persistence/cache/chunk, behind the flag. No source_kind or behavior uncovered. |
| **M5 — Hard cutover complete** | Wave 4 (T9, post-flip) | Default flipped; ~30 callers re-pointed; legacy files deleted; transitional flags retired (each at its own migration); legacy tests migrated/deleted; docs/CLAUDE/skills updated + ADR statuses flipped; `make lint && type && test` ≥ 80%; import-linter green incl. all new contracts; no dangling `SUMMARIZE_GRAPH_ENABLED`. |

**CI-green definition (every PR):** `make lint` (ruff ≥ 0.15.13, B006/B023) + `make type` (mypy 3.13) + `pytest` ≥ 80% coverage + `lint-imports` (all contracts incl. new layered/port-only) + `make check-openapi-drift`/`check-openapi-validate` + lockfile-drift + radon + security (Bandit/pip-audit/Safety/Gitleaks) + web build/test + Docker image build.

---

## 5. Risk Register (cross-cutting) + Rollback

### Top cross-cutting risks + mitigations

| # | Risk | Mitigation |
|---|---|---|
| R1 | **`pyproject.toml` / `.importlinter` / `core.py` / `nodes/*` / `url_processor.py` are multi-track hot files.** | Serialize: T1→T3 (Wave 0), T2 owns the enum, T5 owns `nodes/*` + `graph.py`, T7 owns the bulk collapse and lands first in Wave 3; T6/T8 coordinate the shared seams with T7. No two tracks edit the same file concurrently. |
| R2 | **Parity net is the entire post-cutover safety (ADR-0013 hard cutover, no revert).** | Build the net BEFORE any caller is cut over; cover ALL source_kinds including the under-documented github + meta; mock the llm_client port and compare shaped dicts; gate M4 strictly. |
| R3 | **IDOR / cross-tenant leak** if any retrieval/ground path omits environment+user_scope+user_id. | Centralize the filter in the single Qdrant adapter so omission is structurally impossible; scope-invariant test per entity_type; ground excludes current request_id. |
| R4 | **Checkpoint contamination / PII** — non-primitive or auth-bearing data in `SummarizeState`. | Strict msgpack serialization test (only guard at runtime); minimal id-based state; redact Authorization; prune job as retention backstop. |
| R5 | **Lockfile drift / supply-chain** — relock without `SAFETY_API_KEY`, or `--extra graph` added to one export site but not the other. | Relock with the key (or via the regenerate-lockfiles workflow); keep Makefile + ci.yml export invocations byte-identical; `uv lock --check` + double-`make lock-uv` diff gate. |
| R6 | **Pool exhaustion** — two drivers in one process (asyncpg + psycopg3). | ADR-0004 authoritative max=5 for the checkpointer pool; document in `docs/vector-index-sync.md` connection-budget table; verify Postgres `max_connections` headroom; checkpointer pool isolated from `Database`. |
| R7 | **Contract drift** — OpenAPI/MCP or distance-vs-similarity numeric drift during retrieval convergence. | Golden byte-equal parity tests per endpoint/tool at default flags; `make check-openapi-drift` clean; reuse exact response-mapping conventions. |
| R8 | **Orphan-collaborator deletion** — deleting `url_processor` removes glue still used by graph nodes. | Per-file grep before every deletion; confirm reusable logic (budget/semaphore/sticky/two-pass/persist/chunk) already migrated into nodes/ports. |
| R9 | **Double-emit terminal done/error** (graph runner vs `BackgroundProgressPublisher`). | Align so each request_id stream terminates on exactly one done/error; bridge lifecycle in the runner, not the node. |

### Rollback (strangler-fig)

- **Every step is independently green and reversible by branch revert** — no step depends on a half-migrated state. Each PR passes the full CI-green gate above before merge.
- **Pre-cutover (Waves 0–3):** all new behavior is behind `SUMMARIZE_GRAPH_ENABLED` / `SUMMARIZE_RAG_ENABLED` / `LANGGRAPH_CHECKPOINT_ENABLED` (all default OFF). Disabling the flag restores the legacy path with zero code change; new infra (checkpointer, ports, retrieval adapter) is additive and dormant.
- **At cutover (Wave 4):** ADR-0013 is a hard cutover (legacy deleted in the same step the default flips). There is no flag-flip rollback after deletion — the **parity net (M4) is the gate**: if any source_kind or behavior is not byte/shape-equal, the cutover does NOT proceed. Pre-deletion, the only rollback is reverting the flip commit (legacy still present); post-deletion, rollback is a forward-fix on the graph path. This is why M4 must be exhaustive and green per source_kind before M5.
- **Flag lifecycle (ADR-0018):** no flag outlives its migration; each is removed at its own cutover with a recorded removal trigger. After M5, `SUMMARIZE_GRAPH_ENABLED` is gone; `SUMMARIZE_RAG_ENABLED` and `LANGGRAPH_CHECKPOINT_ENABLED` retire with T6/T2 respectively.
