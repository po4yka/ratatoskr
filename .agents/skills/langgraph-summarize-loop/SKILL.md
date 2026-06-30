---
name: langgraph-summarize-loop
description: Debug the LangGraph summarize graph (the sole summarize path) -- node walk, validate/repair retry loop, and the LLM attempt trail. Trigger keywords -- LangGraph, summarize graph, graph node, retry, repair loop, attempt_trigger, attempt_index, graph_node, validation failure, self-correction, summarization agent.
version: 2.0.0
allowed-tools: Bash, Read, Grep
---

# LangGraph Summarize Loop

Trace and debug the **LangGraph summarize `StateGraph`** -- since the T9 cutover the
graph is the **sole** summarize path (the legacy `url_processor` /
`pure_summary_service` / `interactive_summary_service` indirection and the
`SUMMARIZE_GRAPH_ENABLED` flag are deleted). Every summarization request walks the
same node spine, and the validate→repair↺validate cycle is the self-correction loop
that used to live in `pure_summary_service`.

## The Graph

The graph is assembled in `app/application/graphs/summarize/graph.py` and runs the
node bodies in `app/application/graphs/summarize/nodes/`. The spine is linear except
for the conditional edge out of `validate`:

```
START → ingest → extract → ground → build_prompt → summarize → validate ──┐
                                                       ▲                   │
                                                       │ (validation_errors)
                                                    repair ◀──────────────┘
                                                       │ (valid)
                                                       ▼
                                          enrich → persist → notify → END
```

| Node | Role |
| ---- | ---- |
| `ingest` | Settle request identity (`correlation_id` / `request_id`, sacred). Normalize URL + `dedupe_hash`. |
| `extract` | Fetch source content via the **extraction port** (`deps.extraction`), which dispatches internally by URL pattern to the scraper chain / YouTube / Twitter / academic / GitHub / meta. No-ops when content was pre-supplied (`source_text`). |
| `ground` | Optional RAG grounding (`SUMMARIZE_RAG_ENABLED`, default off): scope-filtered top-k retrieval via the unified retrieval port → writes the "RELATED PRIOR SUMMARIES (reference only)" anti-contamination block into state. |
| `build_prompt` | Assemble the system + user prompt (en/ru lockstep), select `max_tokens`, concatenate `grounding_block`. |
| `summarize` | Structured-output LLM call via the `llm_client` port (`summarize_with_instructor`, or the token-streaming path when `stream` is set). Writes an `llm_calls` record. |
| `validate` | Run `app/core/summary_contract.py::validate_and_shape_summary`. Valid → clear errors (router → `enrich`); contract `ValidationError` → populate `validation_errors` (router → `repair`). |
| `repair` | Re-prompt to fix the contract errors (Instructor reask). Bounded by `MAX_REPAIR_ATTEMPTS`; on exhaustion raises `CallBudgetExceeded` → terminal path. Writes an `llm_calls` record. Loops back to `validate`. |
| `enrich` | Optional two-pass enrichment (`two_pass_enabled`); byte-identical no-op otherwise; never raises. |
| `persist` | persist-everything: write the `summaries` row + flip the request to COMPLETED, write every accumulated `llm_calls` record, and synchronously index the read-your-writes Qdrant point (ADR-0012). |
| `notify` | Spine terminus -- terminal user notification / interaction update + the streaming `DONE` stage trigger. Intentional clean no-op body. |

Deps are bound to nodes via `functools.partial` at build time and live in graph
*config*, never in serializable state (ADR-0011). `thread_id == correlation_id` and
`recursion_limit` are set **per-invocation** in `run_summarize_graph`, not at
`compile`.

## The Retry / Repair Loop

The `validate → repair → validate` cycle is the self-correction loop:

- Bounded by `MAX_REPAIR_ATTEMPTS = 3` (`app/application/graphs/summarize/state.py`).
- langgraph's per-invocation `recursion_limit` (default 25) is an **independent**
  backstop -- the repair budget is the primary terminator.
- When the repair budget is exhausted, the `repair` node raises `CallBudgetExceeded`.
  A node exception, langgraph's `GraphRecursionError`, and `CallBudgetExceeded` **all**
  route to the **single** terminal-failure path (`lifecycle.py::route_terminal_failure`)
  -- no parallel error path (ADR-0011). The user message carries `Error ID:
  <correlation_id>`.

## The Attempt Trail

Every LLM invocation lands in `llm_calls` with two queryable fields:

| Column | Meaning |
| ------ | ------- |
| `attempt_index` | 1-based monotonic counter per `request_id` |
| `attempt_trigger` | Postgres enum (see below) |

### `attempt_trigger` values

| Value | Meaning |
| ----- | ------- |
| `graph_node` | **Active value.** LLM call issued by a node of the summarize graph (`summarize` + `repair`). Since the cutover this is what a normal summarization writes. |
| `initial` | First call on a fresh request (request lifecycle). |
| `user_retry` | First call of a request cloned by the mobile-API retry action. |
| `auto_backfill` | Reserved -- no active code path writes it. |
| `repair_loop` | Legacy JSON-repair self-correction trigger; the graph's repair node uses `graph_node`. |
| `stream_fallback_retry` | Reserved (streaming reuses the in-flight `LLMCall` row rather than inserting a new one). |
| `webwright_tool` | Reserved -- the Webwright enricher (Path C) that wrote it was removed. |

A healthy summarization for a hard URL looks like: `graph_node` × N rows
(`attempt_index` 1..N, `status` `ok` on the last) -- the first is the `summarize`
node, the rest are `repair` re-prompts.

A pathological one: `graph_node` repeated `1 + MAX_REPAIR_ATTEMPTS` times with all
errors -- the graph hit its repair budget and routed to the terminal-failure path.

## Dynamic Context

```bash
!docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -t -c "SELECT attempt_trigger, count(*) FROM llm_calls WHERE created_at > now() - interval '24 hours' GROUP BY attempt_trigger ORDER BY count DESC"
```

## Common Queries

### Full attempt trail for a request

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT attempt_index, attempt_trigger, model, status,
          tokens_prompt, tokens_completion, cost_usd,
          left(error_text, 80) AS err_preview, created_at
     FROM llm_calls
    WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>')
    ORDER BY attempt_index;"
```

### Requests that exhausted the repair budget

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT r.correlation_id, r.input_url, count(*) AS attempts
     FROM llm_calls l JOIN requests r ON r.id = l.request_id
    WHERE l.attempt_trigger = 'graph_node'
      AND l.created_at > now() - interval '24 hours'
    GROUP BY r.correlation_id, r.input_url
   HAVING count(*) >= 4
    ORDER BY attempts DESC LIMIT 20;"
```

(`1 + MAX_REPAIR_ATTEMPTS = 4` rows means the loop ran to budget.)

### What did the validator reject?

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -At -c \
  "SELECT attempt_index, error_text
     FROM llm_calls
    WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>')
    ORDER BY attempt_index;"
```

The repair-attempt `error_text` contains the validator feedback that's fed back into
the next prompt.

### View the prompt sent on a specific attempt

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -At -c \
  "SELECT request_messages_json
     FROM llm_calls
    WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>')
      AND attempt_index = <n>;" \
  | python -m json.tool
```

## Key Files

- **Graph assembly + invocation**: `app/application/graphs/summarize/graph.py`
  (`build_summarize_graph`, `run_summarize_graph`).
- **Nodes**: `app/application/graphs/summarize/nodes/` (`ingest`, `extract`,
  `ground`, `build_prompt`, `summarize`, `validate`, `repair`, `enrich`, `persist`,
  `notify`).
- **State**: `app/application/graphs/summarize/state.py` (`SummarizeState`,
  `MAX_REPAIR_ATTEMPTS`).
- **Terminal failure / budget**: `app/application/graphs/summarize/lifecycle.py`
  (`route_terminal_failure`, `CallBudgetExceeded`).
- **URL-flow facade (entrypoint)**: `app/adapters/content/graph_url_processor.py`
  (`GraphURLProcessor`).
- **Streaming runner + bridge**: `app/di/graphs.py` (`run_summarize_graph_streamed`),
  `app/adapters/content/streaming/graph_event_bridge.py`.
- **Structured LLM call**: `app/application/services/summarization/graph_llm.py`
  (`summarize_with_instructor`), `app/adapters/openrouter/openrouter_client.py`.
- **Validation**: `app/core/summary_contract.py`, `app/core/summary_schema.py`.
- **Checkpoint persistence**: `app/infrastructure/checkpointing/`.
- **DB table**: `llm_calls`.
- **Architecture docs**: `docs/explanation/multi-agent-architecture.md`,
  `docs/decisions/0010-graph-orchestration-layering.md`,
  `docs/decisions/0011-graph-runtime-contract.md`,
  `docs/decisions/0015-summarization-pipeline-target-architecture.md`.

## The Facade Entrypoint: GraphURLProcessor

`app/adapters/content/graph_url_processor.py::GraphURLProcessor` is the **sole
production entrypoint** post-T9. It exposes two public methods:

| Method | What it does |
| ------ | ------------ |
| `handle_url_flow` | Full interactive/silent/batch URL flow: cache short-circuit, typing indicator, OTel span, in-flight gauge + latency metric, `Request` row creation, `RequestProcessingJob` crash-recovery lease, graph invocation, terminal-failure notification, post-summary tasks. |
| `summarize` | Content-only path: caller pre-extracts text and passes `content_text`; `request_id=None`; the graph skips extraction (`extract` node no-ops when `source_text` is already set) and runs summarize → validate → repair → enrich only. The `persist` node short-circuits **every** DB write when `request_id is None`. Returns the shaped summary dict. On success, runs LLM metadata-completion (title/author/dates) + RAG-field enrichment as best-effort post-steps. |

The `RequestProcessingJob` crash-recovery lease is owned by `handle_url_flow` (not
the graph nodes). If the process crashes between job creation and notify, the lease
expires and the job becomes reschedulable.

## Detected Language and Vision Routing

**Detected language**: the `ingest` node resolves `lang` from `request.chosen_lang`
(user override) or from language detection on the URL/content. `build_prompt` selects
`summary_system_{lang}.txt` accordingly. Both `en` and `ru` prompt files must be
updated together -- a lockstep test enforces shared grounding-guard wording.

**Article vision**: when `ARTICLE_VISION_ENABLED=true` and the extracted article
contains ≥ `ARTICLE_VISION_MIN_IMAGES` images (filtered by
`VISION_ROUTING_ROLE_FILTER_ENABLED` to exclude decorative OG/header images), the
`extract` or `build_prompt` node routes the call to `ATTACHMENT_VISION_MODEL`
(from `ratatoskr.yaml`) instead of the text-only model. Articles with fewer images
after filtering take the standard text path.

## Redis Summary Cache

`app/adapters/content/summary_cache_adapter.py::SummaryCacheAdapter` (implements
`SummaryCachePort`) is checked by `handle_url_flow` **before** graph invocation. A
cache hit returns the stored summary immediately, skipping all graph nodes. Cache
keys are env-scoped:

```
("llm", environment, user_scope, prompt_version, lang_key, url_hash)
```

TTL is controlled by `REDIS_LLM_TTL_SECONDS` (default `7200` seconds = 2 h).
Flushing stale cache entries:

```bash
redis-cli KEYS "llm:*" | xargs redis-cli DEL
```

## Important Notes

- **langgraph is confined** to the graph-assembly seam (`graph.py`) plus
  `app/di/graphs.py`; it is imported **lazily inside functions** so `app.*` modules
  stay importable in the CI envs that lack the optional `graph` extra. Node bodies
  must not import langgraph.
- `attempt_trigger` is a Postgres enum -- adding a value needs a migration that
  `ALTER`s the type (see the `alembic-migrations` skill). `graph_node` is migration
  `0036`.
- `MAX_REPAIR_ATTEMPTS` and `recursion_limit` are two independent budgets; the repair
  budget is the primary terminator and the recursion limit only catches a runaway.
- The graph persists checkpoints when `LANGGRAPH_CHECKPOINT_ENABLED=true` (default
  off); resuming a partially-failed request reuses the prior state. State holds
  serializable primitives only -- never a port, session, or live object (ADR-0011).
- Streaming reuses the same in-flight `LLMCall` row, so a stream fallback does not
  reset `attempt_index`.
- Cost reconciliation: `tokens_prompt` + `tokens_completion` + `cost_usd` are
  persisted on every row including failed attempts (persist-everything).
- Update both `app/prompts/summary_system_en.txt` AND `summary_system_ru.txt` (and
  the `_instructor` variants) when changing prompt behavior -- `build_prompt` reads
  whichever matches the request language, and an en+ru lockstep test asserts the
  shared grounding-guard wording.
- The legacy `url_processor.py` / `pure_summary_service.py` /
  `interactive_summary_service.py` and the `SUMMARIZE_GRAPH_ENABLED` flag are
  **deleted** (T9 cutover). There is no flag gate; the graph is the only path.
