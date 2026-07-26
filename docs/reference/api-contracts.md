# External API contracts

Ratatoskr isolates external systems behind adapters. The adapter protocol, request/response shaping, error mapping, and persistence code are the authoritative local contract; provider documentation remains authoritative for the remote API.

## Contract map

| System | Local ownership | Durable evidence |
|---|---|---|
| LLM providers | `app/adapters/llm/protocol.py`, `app/adapters/llm/factory.py`, `app/adapters/openrouter/`, `app/adapters/llm/openai/`, `app/adapters/llm/anthropic/` | `llm_calls` |
| Generic extraction | `app/adapters/content/scraper/` and provider adapters | `crawl_results` |
| YouTube | `app/adapters/youtube/` and yt-dlp/transcript adapters | `video_downloads`, requests/crawl records |
| Twitter/X | `app/adapters/twitter/` | request/crawl records and X metadata where applicable |
| Telegram | `app/adapters/telegram/`; digest userbot under `app/adapters/digest/` | `telegram_messages`, digest tables |
| GitHub | `app/adapters/github/`, auth router, and sync task | repository/integration tables, LLM calls |
| Qdrant | `app/infrastructure/vector/` and reconciliation adapters | PostgreSQL embedding/index metadata; Qdrant is derived |
| Redis/Taskiq | `app/infrastructure/cache/`, `app/tasks/` | terminal Taskiq failures in `taskiq_failed_jobs`; other Redis state is ephemeral |
| Webhooks/export | API/application services plus integration adapters | webhook/export delivery logs |

## LLM contract

Callers depend on `LLMClientProtocol`, not a provider SDK. Provider selection is configured by `LLM_PROVIDER`; structured output is shaped through `SummaryContractDescriptor`. Model fallback, timeouts, usage metadata, and provider-specific response modes stay inside the adapter/workflow boundary.

Every attempt that belongs to a persisted workflow records model, trigger/index, status, latency, token/usage data when supplied, and sanitized error/response information. Authorization headers are never persisted.

See [LLM Providers](llm-providers.md) and [Summary Contract](summary-contract.md).

## Extraction contract

Generic providers implement `ContentScraperProtocol` and return the shared result shape used by the chain and persistence layer. Unsupported URLs skip URL-scoped providers; errors and low-quality results fall through until a winner or terminal failure. Platform-specific extractors have their own routing but preserve correlation and persistence requirements.

Cloud Firecrawl is not part of the generic article extraction contract. The configured Firecrawl rung targets the self-hosted sidecar. See [Scraper Chain](../explanation/scraper-chain.md).

## Error and retry policy

- Validate input before sending it to a provider.
- Apply SSRF/host policies at every network-capable boundary that accepts user-controlled targets.
- Use bounded timeouts and retries; honor remote retry guidance when safe.
- Map expected provider failures to stable local categories while preserving sanitized diagnostics.
- Do not retry authentication, authorization, validation, or deterministic contract failures without a state/input change.
- Preserve the original correlation ID across fallbacks and retries.

## Security and privacy

Secrets come from secret-marked configuration or encrypted user integration records. Logs and persisted request metadata must redact authorization, cookies, tokens, and signed URLs. Debug payload previews remain bounded and sanitized. Connected-user operations enforce user ownership at the repository/service boundary.

## Adding or changing an integration

1. change the owning protocol/adapter and configuration model;
2. add contract tests for success, timeout, rate limit, malformed response, and redaction;
3. persist the attempt/outcome with correlation context;
4. update both language prompts if behavior changes LLM instructions;
5. update generated OpenAPI when an HTTP surface changes;
6. update the focused integration guide rather than duplicating remote API examples here.

Last audited: 2026-07-15.
