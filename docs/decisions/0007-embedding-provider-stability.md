# ADR 0007: Embedding-provider stability & reindex strategy

**Date:** 2026-06-15
**Status:** Accepted.

## Context

`EMBEDDING_PROVIDER` selects between `local` (sentence-transformers, 384-dim) and `gemini` (Gemini Embedding, 768-dim). Switching provider or dimension **invalidates all existing vectors**: the Qdrant collection name encodes the embedding space and the dimensions differ. With RAG grounding (ADR-0005) becoming load-bearing, an unplanned switch silently breaks retrieval.

## Decision

- The embedding provider and dimension are a **deployment-stable** choice. Changing them is a **migration, not a config flip**.
- A switch requires a full reindex into a new collection (the collection name already namespaces by embedding space, so old and new coexist without collision) followed by a read cutover. Run the backfill/reconcile path (`app/cli/backfill_vector_store.py` + `app/cli/reconcile_vector_index.py`) against the new collection **before** flipping reads.
- Default remains `local` (no external dependency, no per-call cost). `gemini` is opt-in for quality.

## Consequences

- RAG quality is tied to the chosen provider; provider changes are scheduled maintenance, not runtime toggles.
- Mixed-dimension collections never collide (namespacing), so a migration can be staged and rolled back.
- Documented in `docs/reference/environment-variables.md` and `docs/cocoindex.md`.

## Alternatives rejected

- **Auto-reembed on switch** — expensive and a foot-gun (a stray env change triggers a full reindex).
- **Store multiple embeddings per item** — storage and complexity cost for a rare operation; YAGNI.
