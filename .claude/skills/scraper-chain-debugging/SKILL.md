---
name: scraper-chain-debugging
description: Debug the current 13-provider content scraper chain and persisted attempt telemetry. Trigger keywords -- scraper, scraping failure, Scrapling, Crawl4AI, Defuddle, Firecrawl self-hosted, Playwright, Crawlee, Webwright, crawl_results, attempt_log, winning_provider.
version: 2.0.0
allowed-tools: Bash, Read, Grep
---

# Scraper Chain Debugging

Diagnose failures in `ContentScraperChain` and its persisted per-request telemetry.

## Default provider order

The source of truth is `DEFAULT_SCRAPER_PROVIDER_ORDER` in `app/config/scraper.py`:

1. `reddit`
2. `hn`
3. `scrapling`
4. `direct_pdf`
5. `crawl4ai`
6. `firecrawl`
7. `defuddle`
8. `cloakbrowser`
9. `playwright`
10. `crawlee`
11. `direct_html`
12. `scrapegraph_ai`
13. `webwright`

`reddit` and `hn` only support their own URL families. `webwright` is an allowlisted, LLM-driven last resort. With `SCRAPER_RACE_ENABLED=true`, eligible free and browser providers may race in parallel; the configured order remains the construction and fallback baseline.

## Persisted telemetry

`crawl_results` has one row per request (`request_id` is unique), not one row per provider. The fallback trail is stored in `attempt_log`; `winning_provider` identifies the accepted provider.

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT request_id, winning_provider, status, http_status, latency_ms,
          attempt_log, error_text
     FROM crawl_results
    WHERE request_id = (
            SELECT id FROM requests WHERE correlation_id = '<correlation_id>'
          );"
```

Recent winner distribution:

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT winning_provider,
          count(*) FILTER (WHERE status = 'ok') AS ok,
          count(*) FILTER (WHERE status <> 'ok') AS err,
          round(avg(latency_ms)) AS avg_ms
     FROM crawl_results
    WHERE updated_at > now() - interval '7 days'
    GROUP BY winning_provider
    ORDER BY ok DESC;"
```

Inspect `attempt_log` for provider order, per-attempt error, latency, and quality rejection. On total exhaustion, `winning_provider` is null and `error_text`/the final attempt explain the failure.

## Triage sequence

1. Resolve the public correlation ID to `requests.id`.
2. Read the single `crawl_results` row and its `attempt_log`.
3. Compare the effective providers with `SCRAPER_PROVIDER_ORDER`, feature flags, forced provider, host allowlists, and runtime profile.
4. Check the relevant sidecar/container only for providers that were actually attempted.
5. Reproduce with `SCRAPER_FORCE_PROVIDER=<token>` when isolating one provider.

## Common checks

- `crawl4ai`, `firecrawl`, `defuddle`, `cloakbrowser`, and `webwright` require their corresponding sidecars/configuration.
- `playwright` and `crawlee` require a working browser runtime.
- `direct_pdf` is selected for PDF content before generic HTML providers.
- `scrapegraph_ai` and `webwright` may incur LLM cost; both are late fallbacks.
- Content below `SCRAPER_MIN_CONTENT_LENGTH` or failing quality gates is rejected even when HTTP succeeds.
- Academic, YouTube, Twitter/X, Reddit, and HN paths may use platform-specific extraction before or around the generic chain; confirm `requests.source_kind` and the attempt log.

## Key files

- Config and provider tokens: `app/config/scraper.py`
- Chain: `app/adapters/content/scraper/chain.py`
- Attempt telemetry: `app/adapters/content/scraper/attempt_log.py`
- Diagnostics: `app/adapters/content/scraper/diagnostics.py`
- Factory/providers: `app/adapters/content/scraper/factory.py`, `app/adapters/content/scraper/*_provider.py`
- DB model: `app/db/models/core.py::CrawlResult`
- Full explanation: `docs/explanation/scraper-chain.md`
