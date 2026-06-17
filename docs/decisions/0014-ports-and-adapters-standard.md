# ADR 0014: Ports-and-adapters as the enforced project standard

**Date:** 2026-06-15
**Status:** Implemented for the summarize graph — graph nodes reach external systems only through `app/application/ports/` (retrieval, extraction, llm_client, stream_sink, requests, summaries, …). Project-wide port adoption beyond the summarize path remains ongoing.

## Context

[ADR-0010](0010-graph-orchestration-layering.md) mandates port-only access for the graph. The committed clean rewrite extends this **project-wide**. Today many adapters/services call across layers directly, and `.importlinter` enforces only coarse `*-no-outward` forbiddens (domain-independence, application-no-outward, infrastructure-no-api, tasks-no-api, content-no-telegram). For clean hexagonal adherence, all cross-layer access should go through application ports, executable-enforced by import-linter.

## Decision

- **Ports-and-adapters is the project standard.** Application logic depends on `app/application/ports/*` interfaces; concrete adapters (`app/adapters/*`, `app/infrastructure/*`) implement them and are injected via `app/di/` (the single composition root). Application never imports adapters/infrastructure directly (already `application-no-outward`); additionally, **adapters and api reach application capabilities through ports / use-cases, not by importing concrete service classes**.
- **Strengthen `.importlinter`:** add a layered contract (`domain` < `application` < {`adapters`, `infrastructure`} < {`api`, `di`, `tasks`}) and contracts forbidding cross-adapter imports of concrete classes except via ports. Each new contract lands with the refactor step that makes it pass.
- **Migration is incremental / strangler-fig** ([ADR-0018](0018-refactor-strategy-and-invariants.md)): introduce a port → route new code through it → migrate existing callers → enforce with a contract. No big-bang PR.

## Consequences

- New/renamed ports for the currently-direct couplings (LLM, retrieval, persistence, extraction, embedding, …); `app/di/` grows as the composition root.
- import-linter contracts become the **executable definition** of the architecture; CI enforces them and prevents regression.
- Large surface, sequenced by ADR-0018; each step is independently green (lint / type / import-linter / tests / coverage).

## Alternatives rejected

- **Ports only for new/graph code** — leaves the architecture half-clean, which is exactly what the rewrite is meant to fix.
- **Big-bang refactor** — unreviewable and unsafe; replaced by strangler-fig sequencing.
