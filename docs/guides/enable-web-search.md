# Enable web-search enrichment

Web-search enrichment lets `WebSearchAgent` identify gaps in extracted content, issue bounded queries through the configured topic-search service, and add dated context to the summary prompt.

## Requirements

- the self-hosted Firecrawl client must be enabled and reachable;
- `WEB_SEARCH_ENABLED=true`;
- the configured LLM adapter must be available for query/gap analysis;
- Redis is optional and used only when the relevant cache path is configured.

The active search client is built from `SCRAPER_FIRECRAWL_SELF_HOSTED_URL`; this guide does not use a DuckDuckGo fallback or cloud Firecrawl key.

## Configure

Start the self-hosted sidecar profile and enable its client:

```bash
FIRECRAWL_SELF_HOSTED_ENABLED=true \
docker compose -f ops/docker/docker-compose.yml \
  --profile with-scrapers up -d --build
```

Set the enrichment controls in `.env` or their YAML equivalents:

```env
WEB_SEARCH_ENABLED=true
WEB_SEARCH_MAX_QUERIES=3
WEB_SEARCH_MIN_CONTENT_LENGTH=500
WEB_SEARCH_TIMEOUT_SEC=10
WEB_SEARCH_MAX_CONTEXT_CHARS=2000
WEB_SEARCH_CACHE_TTL_SEC=3600
```

Restart the bot/worker services after configuration changes.

## Runtime behavior

`app/adapters/content/search_context_enricher.py` skips when enrichment is disabled, the topic-search service is unavailable, or content is shorter than the configured minimum. Otherwise it invokes `WebSearchAgent`, which decides whether search is valuable, produces at most the configured number of queries, and asks `TopicSearchService` to normalize self-hosted Firecrawl search results.

Search failures degrade to an empty context and do not bypass the normal summary contract. Agent LLM calls are persisted with their request/correlation context where a repository is available.

## Verify

Process a current-event article and inspect logs/metrics for:

- `web_search_context_injected` on success;
- `web_search_skipped_no_service` when the self-hosted search client was not built;
- `web_search_skipped_short_content` for content below the threshold;
- `web_search_enrichment_failed` for a sanitized provider/agent failure.

Prometheus decisions and query-result histograms are emitted by helpers in `app/observability/metrics.py`. Use persisted LLM usage/cost data for the actual provider expense; fixed token-price estimates become stale quickly.

## Disable

```env
WEB_SEARCH_ENABLED=false
```

There is no documented per-request `/summarize --no-web-search` override in the current command surface.

See [Environment Variables](../reference/environment-variables.md), [Graph and Agent Architecture](../explanation/multi-agent-architecture.md), and [Troubleshooting](../reference/troubleshooting.md).
