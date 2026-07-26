# Design philosophy

Ratatoskr favors explicit workflows, durable evidence, and replaceable infrastructure over hidden orchestration. These principles describe the current codebase and the constraints contributors should preserve.

## Deterministic workflow, focused agents

Ordinary URL summaries use one LangGraph state machine: ingest, extract, ground, build prompt, summarize, validate/repair, enrich, persist, notify. This makes retries, terminal states, persistence, and user notification observable. Focused agents remain appropriate for web search, multi-source aggregation, relationship analysis, and repository analysis; they are not alternate summary pipelines.

See [Graph and Agent Architecture](multi-agent-architecture.md).

## Ports and adapters

Domain and application code depend on protocols, not Telegram, FastAPI, SQLAlchemy, Qdrant, or a particular LLM vendor. Runtime composition belongs in `app/di/`. This keeps provider replacement and targeted testing local while preventing presentation handlers from becoming orchestration layers.

See [Architecture Overview](architecture-overview.md#layering-quick-reference).

## PostgreSQL is authoritative

Requests, processing jobs, scraper attempts, LLM calls, summaries, user content, integrations, and task failures are durable PostgreSQL records. Qdrant is a derived retrieval index and Redis is coordination/cache infrastructure. Neither may silently become the only copy of user state.

`app/db/session.py::Database` is the application session boundary. Schema evolution uses SQLAlchemy models plus Alembic migrations.

## Strict output contracts

LLM output is untrusted until it satisfies the summary descriptor and schema. Prompt selection, provider response formats, validation, repair, and compatibility shaping remain bundled through `SummaryContractDescriptor`. Repair is bounded; a failed contract is reported and persisted rather than guessed into validity.

See [Summary Contract Design](summary-contract-design.md).

## Provider choice is configuration

The LLM boundary supports OpenRouter, OpenAI, Anthropic, and Ollama. OpenRouter is the default deployment path, not a domain dependency. Model selection lives in `ratatoskr.yaml` or matching environment variables, with no hidden model default in application code for the selected provider.

Extraction follows the same principle: the chain owns ordered fallbacks and provider-specific adapters own transport details. Platform-specific extractors bypass the generic chain only through explicit routing.

## Bounded degradation

External services fail. Ratatoskr uses bounded retries, provider fallback, timeouts, circuit/lock behavior where appropriate, and explicit terminal states. Degradation must preserve correlation IDs and failure evidence. Security, validation, authorization, and quality gates are never weakened merely to produce a nominal success.

## Async work has explicit ownership

Request-path I/O is asynchronous. Taskiq with Redis owns background workers and schedules recurring jobs. A task should be idempotent where practical, use distributed locking when concurrent execution would corrupt state, and persist terminal failures when retry budgets are exhausted.

## Owner-first security, defense in depth

Telegram is allowlist-first and typical deployments are owner-operated. HTTP and MCP surfaces can support additional configured identities, so user-owned queries retain `user_id` predicates. Secrets are supplied through configuration or encrypted storage, and authorization material is redacted before logging.

## Evidence-driven observability

A correlation ID joins user-visible errors, logs, PostgreSQL rows, scraper attempts, LLM calls, graph state, and task activity. Metrics and traces complement that durable evidence; they do not replace it. Operational documentation must name the exact check when a result cannot be verified.

See [Observability Strategy](observability-strategy.md).

## Small coherent changes

Prefer the smallest root-cause change that preserves established contracts. Add abstractions after repeated need, not in anticipation of it. Avoid parallel implementations for the same workflow; migrate callers and remove the old path when a replacement becomes canonical.

## Documentation follows executable truth

Generated OpenAPI, model/migration pairs, configuration classes, route registration, and tests outrank hand-maintained inventories. Documentation should link to those sources, avoid volatile line counts and external-repository internals, and keep historical ADRs distinct from current operating guidance.

Last audited: 2026-07-15.
