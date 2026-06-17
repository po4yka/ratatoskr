# ADR 0016: Unified retrieval subsystem

**Date:** 2026-06-15
**Status:** Implemented — the unified retrieval port (`app/application/ports/retrieval.py`) backs the graph `ground` node's scope-filtered top-k lookup.

## Context

Vector retrieval is implemented three times — `StoreVectorSearchService` (summaries), `RepositorySearchService` (repos), and the MCP `SemanticSearchService` (`semantic_search` / `find_similar`) — plus the new graph RAG need (ADR-0005). Each re-derives query embedding, Qdrant query, scope filtering, and result hydration. The clean rewrite is the moment to converge them behind one port.

## Decision

- **One port:** `app/application/ports/retrieval.py` — `retrieve(query | vector, entity_type, scope, top_k, filters) -> hits` and `find_similar(entity_type, id, top_k)`. **One Qdrant-backed adapter** implements it, reusing the existing Qdrant client + embedding service.
- **All consumers use it:** the graph `ground` node (ADR-0005/0012), MCP `semantic_search` / `find_similar`, and the API `/search/*` endpoints. The three existing services become thin callers of the port or are deleted.
- The port **centralizes the mandatory scope filter** (`user_scope` / `environment`), reranking, and query expansion — currently duplicated and scattered.

## Consequences

- Removes three-way duplication; one place to get scope-safety, reranking, and read-your-writes freshness (ADR-0012) right.
- API/MCP search behavior must remain contract-stable (OpenAPI unchanged) — guarded by parity/contract tests (ADR-0018).
- The `entity_type` discriminator (summary / repository / …) is a first-class port parameter, so adding a retrievable entity type is a port call, not a new service.

## Alternatives rejected

- **Give the graph its own retrieval, leave the rest** — perpetuates the three-way duplication the rewrite is meant to remove.
- **Defer unification** — the rewrite is the cheapest time to converge; later means migrating four callers instead of three.
