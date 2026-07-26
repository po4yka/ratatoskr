# Frequently asked questions

## What is Ratatoskr?

Ratatoskr is a self-hosted Telegram/API service that extracts web pages, videos, forwarded posts, repositories, feeds, and related sources; produces structured summaries; and stores them in a searchable PostgreSQL archive with optional Qdrant retrieval.

## What content does it handle?

Dedicated adapters cover YouTube, Twitter/X, academic papers, Telegram forwards, GitHub repositories, RSS/digest sources, and attachments. Generic URLs use the [13-provider scraper chain](scraper-chain.md). Mixed-source aggregation can combine multiple source items with provenance.

## Which LLM providers are supported?

`LLM_PROVIDER` supports `openrouter`, `openai`, `anthropic`, and `ollama`. OpenRouter is the default and has the broadest fallback/routing behavior. Direct adapters are available when billing, compliance, or local inference requirements call for them. See [LLM Providers](../reference/llm-providers.md).

## Which values are required for the default Compose setup?

Telegram requires `API_ID`, `API_HASH`, `BOT_TOKEN`, and `ALLOWED_USER_IDS`. Compose requires `POSTGRES_PASSWORD` and a matching `DATABASE_URL`. The selected LLM provider requires its key/model settings; the default OpenRouter path uses `OPENROUTER_API_KEY` plus model choices supplied by `config/ratatoskr.yaml` or environment overrides.

See [Quickstart](../guides/quickstart.md) and [Environment Variables](../reference/environment-variables.md).

## Is Firecrawl cloud required?

No. Article extraction uses in-process providers and optional self-hosted sidecars. The `with-scrapers` profile includes self-hosted Firecrawl and its dependencies alongside Crawl4AI, Defuddle, and CloakBrowser. No cloud Firecrawl API key is required for the generic article chain.

## Can it run without Docker?

Yes, but PostgreSQL, Redis, Qdrant, browser dependencies, and Taskiq processes still need equivalent local or remote services. Docker Compose is the maintained production topology. See [Local Development](../guides/local-development.md).

## Can it run on Raspberry Pi?

Yes. The maintained workflow builds Linux/ARM64 images on the development machine and streams them to the Pi; the Pi does not build images. Heavy browser/LLM and local embedding workloads may be better hosted elsewhere. Use `make pi-deploy` and the `pi-deploy` skill.

## Is it single-user?

Telegram is allowlist-first and typical deployments are owner-operated. Multiple configured identities can use HTTP/MCP surfaces, and user-owned queries retain `user_id` filters. Ratatoskr is not a public self-service SaaS control plane; use separate deployments when strict tenant isolation is required.

## Does self-hosting keep all data local?

Persistent application data remains under operator control, but selected external services receive the data necessary to perform their job. Telegram sees bot traffic; a remote LLM provider sees submitted prompt/content; GitHub and other connected providers see their API requests. Use local Ollama and self-hosted extraction where those transfers are unacceptable.

## How are secrets handled?

Secret-marked configuration comes from environment/deployment secret storage, not YAML. Some user integration tokens and browser sessions are encrypted at rest. Authorization headers, tokens, and cookies must be redacted from logs. Rotation procedures are in [Secret Rotation](../runbooks/secret-rotation.md).

## Does it have a web or mobile client?

This repository provides FastAPI, generated OpenAPI, the browser extension, packaged CLI sources, and compiled web assets. Editable web and KMP client sources live in separate repositories. See [Mobile API](../reference/mobile-api.md) and [Web Frontend Integration](../reference/frontend-web.md).

## How much does it cost?

The software is open source. Runtime cost depends on the selected LLM/model, token volume, hosting, storage, and optional browser/search services. Provider prices and free-model availability change frequently; use current provider dashboards and persisted `llm_calls` usage/cost fields rather than fixed estimates in documentation.

## How do I reduce cost and latency?

Measure first, then tune model/fallback selection, provider ordering, timeouts, concurrency, caching, media retention, and optional enrichment. Keep cheap deterministic extractors ahead of browser/LLM-driven rungs. See [Optimize Performance](../guides/optimize-performance.md).

## Where do I start debugging?

Copy the user-visible Error ID, locate its request/job, then inspect ordered scraper and LLM attempts. See [Observability Strategy](observability-strategy.md) and [Troubleshooting](../reference/troubleshooting.md).

Last audited: 2026-07-15.
