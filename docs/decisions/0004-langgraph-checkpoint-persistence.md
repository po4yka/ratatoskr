# ADR 0004: LangGraph checkpoint persistence — psycopg3 pool alongside asyncpg

**Date:** 2026-06-15
**Status:** Implemented — the psycopg3 checkpoint pool ships in `app/infrastructure/checkpointing/` and the summarize graph compiles with the checkpointer (`app/application/graphs/summarize/graph.py`).

## Context

ADR-0001 re-adopts LangGraph with `langgraph-checkpoint-postgres` for resumable summarize runs. That library is **psycopg3-only**: `AsyncPostgresSaver` uses `psycopg.AsyncConnection` / `psycopg_pool.AsyncConnectionPool` and does not support asyncpg. The application's sole DB entry point (`app/db/session.py::Database`) is SQLAlchemy 2.0 + asyncpg. We must decide how the two drivers coexist and where checkpoint state lives.

## Decision

- Run a **dedicated psycopg3 `AsyncConnectionPool` for the checkpointer only** (small: `min_size=1`, `max_size=5`, `autocommit=True`, `row_factory=dict_row`). It does **not** replace or route through `Database`; the asyncpg pool remains the sole entry point for application data.
- Checkpoint tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) are created by `AsyncPostgresSaver.setup()` at application startup (FastAPI/bot lifespan), in a **dedicated `langgraph` Postgres schema** (via `search_path`). They are **NOT Alembic-managed** — Alembic owns the `public` app schema only.
- `thread_id = correlation_id` (sacred), so resumable runs preserve the correlation ID.
- Set `LANGGRAPH_STRICT_MSGPACK=true` (no arbitrary-module deserialization from checkpoint blobs).

## Retention & redaction (load-bearing)

Checkpoint rows may contain article content and LLM request/response payloads — the same sensitive data the rest of the system governs:

- Never persist `Authorization` headers into checkpoint state (reuse existing redaction helpers).
- Prune checkpoints on a schedule, aligned with the AuditLog 90-day ceiling ([auth memo](2026-05-17-auth-security-second-wave.md) Decision 3); ideally delete a run's checkpoints on its successful terminal node. A nightly Taskiq job drops checkpoints for runs older than the window.

## Consequences

- Two Postgres drivers in-process (asyncpg + psycopg3) and two pools. Verified compatible (separate C extensions, independent pools). The connection-budget math in `docs/vector-index-sync.md` must include this pool.
- A non-Alembic schema exists; ops must know `langgraph.*` tables are managed by the checkpointer (`.setup()`) and are droppable to reset graph state.
- Gated OFF with LangGraph itself (`SUMMARIZE_GRAPH_ENABLED` / `LANGGRAPH_CHECKPOINT_ENABLED`).

## Alternatives rejected

- **Route checkpoints through asyncpg** — unsupported by `langgraph-checkpoint-postgres`.
- **Alembic-manage the checkpoint tables** — couples our migrations to a third-party schema that `.setup()` already versions.
- **In-memory checkpointer** — loses cross-restart resumability, which is the entire justification in ADR-0001.
