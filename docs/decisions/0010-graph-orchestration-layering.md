# ADR 0010: Graph orchestration — layer placement, node→port boundary, dependency discipline

**Date:** 2026-06-15
**Status:** Implemented — the summarize graph lives in `app/application/graphs/summarize/`; langgraph is confined to graph-assembly (`graph.py`) plus `app/di/graphs.py`, and nodes reach external systems only through `app/application/ports/`. `application-no-outward` import-linter contract stays green.

## Context

The LangGraph summarize graph (ADR-0001) must call LLM, retrieval, and persistence. The repo enforces layering via `.importlinter`, notably **`application-no-outward`** (the `application` layer must not import `adapters` or `api`). `app/application/ports/` already provides the seam (`llm_client`, `summaries`, `search`, …); no orchestration directory exists yet, so placement is an open decision with real layering consequences.

## Decision

- **Placement:** the graph definition and nodes live in `app/application/graphs/summarize/` (the application layer).
- **Node→dependency boundary:** nodes depend **only on application ports** — `ports/llm_client.py`, `ports/summaries.py`, and a **new `ports/retrieval.py`** (RAG). Nodes never import `app.adapters.*` or `app.infrastructure.*` directly, preserving `application-no-outward`.
- **Composition:** concrete adapters (OpenRouter, Qdrant retrieval, repositories) are wired into the port-typed node dependencies at composition time in `app/di/` (a new `app/di/graphs.py`).
- **Dependency discipline (folds the former "lock-in" candidate):** use **only `langgraph` + `langchain-core`**. Node business logic is a plain `async def(state, deps) -> dict`; LangGraph appears **only** in the graph-assembly module (`StateGraph`, edges, `compile`). The framework is therefore swappable with bounded blast radius.
- **Import-linter note:** `langgraph` / `langchain_core` are third-party libraries, not an inner/outer project layer — importing them from `application` does **not** violate any contract (only `app.adapters` / `app.api` imports are forbidden). Confirmed against `.importlinter`.

## Consequences

- A new `ports/retrieval.py` (text + lang → top-k hydrated docs) is added; its Qdrant-backed implementation lives in `app/infrastructure/` (or `app/adapters/`) and is injected via DI.
- import-linter stays green with **no new contract** (the graph is inside `application` and obeys `application-no-outward`).
- The graph-assembly module is the single LangGraph-coupled surface; node logic stays framework-agnostic and unit-testable without LangGraph.

## Alternatives rejected

- **`app/orchestration/` new top-level layer** — more ceremony and a new import-linter contract for no benefit at this scope.
- **Graph in `app/adapters/content/`** — lets nodes call adapters directly, but demotes orchestration to the adapter layer and bypasses the port seam, eroding the DDD boundary.
