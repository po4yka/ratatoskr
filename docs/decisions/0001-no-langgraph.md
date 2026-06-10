# ADR 0001: Remove LangGraph/LangChain optional dependency

**Date:** 2026-06-10
**Status:** Accepted

## Context

The `langgraph` optional dependency group (`pip install -e ".[langgraph]"`) pulled in `langchain`, `langgraph`, `langgraph-checkpoint-postgres`, and `psycopg[binary]`. It was originally added to explore LangGraph as a durable task-execution backend with Postgres checkpointing.

A full scan of the codebase (app/, tests/, tools/) found zero Python import statements for any of these packages. The integration was never shipped: no code in the repository depends on `langchain`, `langgraph`, or `langgraph_checkpoint`.

## Decision

Remove the `langgraph` optional-dependency group from `pyproject.toml` and add a ruff `banned-api` entry to prevent silent re-introduction.

## Rationale

- **Taskiq already covers durable execution.** The project uses Taskiq with a Redis broker for all background and scheduled tasks. Adding a second durable-execution framework would increase operational complexity with no benefit.
- **Postgres checkpointing overhead.** `langgraph-checkpoint-postgres` requires a dedicated schema and connection pool. Taskiq's Redis broker serves the same purpose with infrastructure already present in the compose stack.
- **Zero active usage.** Dead optional dependencies inflate the dependency surface and slow `uv lock` resolution without providing any capability.

## Consequences

Any future evaluation of LangGraph must be preceded by an update to this ADR and a deliberate re-addition to `pyproject.toml`.
