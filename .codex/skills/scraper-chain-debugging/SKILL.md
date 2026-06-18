---
name: scraper-chain-debugging
description: Debug the multi-provider content scraper chain. Trigger keywords -- scraper, scraping failure, Scrapling, Crawl4AI, Defuddle, Firecrawl self-hosted, Playwright, Crawlee, Scrapegraph, crawl_results, content extraction empty, provider fallback.
version: 1.0.0
allowed-tools: Bash, Read, Grep
---

# Scraper Chain Debugging

Diagnose failures in the ordered content-scraper fallback chain.

## Provider Order (default)

1. `scrapling` -- in-process, primary
2. `crawl4ai` -- self-hosted Docker sidecar (`SCRAPER_CRAWL4AI_URL`)
3. `firecrawl` -- self-hosted (`FIRECRAWL_SELF_HOSTED_URL`); cloud API is used ONLY by `TopicSearchService` web search, not the chain
4. `defuddle` -- self-hosted (`SCRAPER_DEFUDDLE_API_BASE_URL`)
5. `cloakbrowser` -- self-hosted stealth-Chromium CDP sidecar (`SCRAPER_CLOAKBROWSER_URL`); for Cloudflare Turnstile, reCAPTCHA v3, FingerprintJS-protected pages
6. `playwright` -- in-process headless
7. `crawlee` -- in-process
8. `direct_html` -- raw httpx fetch
9. `scrapegraph_ai` -- last-resort LLM-driven extractor

Each provider logs success/failure with a `scraper` context field. The chain stops at the first success.

## Dynamic Context

```bash
!docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -t -c "SELECT scraper_used, count(*) FILTER (WHERE status = 'ok') AS ok, count(*) FILTER (WHERE status <> 'ok') AS err FROM crawl_results WHERE created_at > now() - interval '24 hours' GROUP BY scraper_used ORDER BY ok DESC"
```

## Common Failure Patterns

### Which provider served a request?

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT scraper_used, status, http_status, latency_ms,
          firecrawl_success, firecrawl_error_code, firecrawl_error_message,
          length(content_markdown) AS md_len
     FROM crawl_results
    WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>')
    ORDER BY created_at;"
```

### Find rows where the chain exhausted (no provider succeeded)

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT request_id, source_url, firecrawl_error_message
     FROM crawl_results
    WHERE status <> 'ok'
    ORDER BY created_at DESC LIMIT 20;"
```

### Provider degradation over time

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT scraper_used,
          count(*) FILTER (WHERE status = 'ok') AS ok,
          count(*) FILTER (WHERE status <> 'ok') AS err,
          round(avg(latency_ms)) AS avg_ms
     FROM crawl_results
    WHERE created_at > now() - interval '7 days'
    GROUP BY scraper_used
    ORDER BY ok DESC;"
```

## Provider-Specific Notes

| Provider | Common failure | Fix |
|----------|----------------|-----|
| `scrapling` | Cloudflare / anti-bot challenge | Falls through to `crawl4ai`; for SSRN/ResearchGate the academic extractor uses patchright stealth |
| `crawl4ai` | Sidecar unreachable | `docker compose -f ops/docker/docker-compose.yml ps crawl4ai`; check `SCRAPER_CRAWL4AI_TIMEOUT_SEC=60` |
| `firecrawl` (self-hosted) | API 5xx / timeout | `docker compose ps firecrawl-api`; honors `SCRAPER_FIRECRAWL_TIMEOUT_SEC=90` |
| `defuddle` | Empty markdown on JS-heavy pages | Expected -- chain falls through to `cloakbrowser` |
| `cloakbrowser` | Sidecar unreachable / CDP connect refused | `docker compose --profile with-scrapers logs cloakbrowser`; check `SCRAPER_CLOAKBROWSER_URL=http://cloakbrowser:9222` resolves. Sidecar runs under `with-scrapers` profile only; without it the chain falls through to `playwright`. |
| `cloakbrowser` | Same domain consistently fails / Cloudflare 1020 / Turnstile loop | Inspect `crawl_results.options_json` for `fingerprint_seed`, `humanize`, `proxy_configured` so the exact stealth config is correlatable. If `humanize` is `skipped`, confirm `SCRAPER_CLOAKBROWSER_HUMANIZE=true` (the in-house bezier fallback runs when the upstream `cloakbrowser.human` helper is not importable). For IP-reputation failures, attach a residential proxy via `SCRAPER_CLOAKBROWSER_PROXY=socks5://...` — it is forwarded per request via the cloakserve `?proxy=` query param. |
| `playwright` | Browser crash / OOM | Restart container; the academic extractor relies on this for paywalled landings |
| `crawlee` | Throttled | Lower concurrency; let `direct_html` catch trivial cases |
| `direct_html` | Returns raw HTML, never structured | Final fallback before scrapegraph; OK for plain pages |
| `scrapegraph_ai` | LLM cost / latency spike | Only fires when everything else failed -- treat as alarm signal |

## Toggling Providers

All providers are independently toggleable via `SCRAPER_<NAME>_ENABLED` env vars (see `app/config/scraper.py`). For triage, disabling a misbehaving provider in `.env` is faster than chasing the bug.

```bash
# Examples
SCRAPER_CRAWL4AI_ENABLED=false
SCRAPER_SCRAPEGRAPH_ENABLED=false
```

## Key Files

- **Protocol**: `app/adapters/content/scraper/protocol.py`
- **Chain orchestrator**: `app/adapters/content/scraper/chain.py`
- **Factory**: `app/adapters/content/scraper/factory.py`
- **Providers**: `app/adapters/content/scraper/<name>_provider.py`
- **Config**: `app/config/scraper.py` (`ScraperConfig`)
- **DB table**: `crawl_results` (one row per provider attempt)
- **Universal output**: `FirecrawlResult` (every provider returns this shape, despite the name)

## Important Notes

- The `scraper_used` column records which provider's row this is, not which one ultimately succeeded for the request -- read all rows for a `request_id`.
- A request can have multiple `crawl_results` rows (one per provider attempted). Order by `created_at` to see the fallback sequence.
- The academic-paper extractor (`app/adapters/academic/`) bypasses the standard chain for arXiv/SSRN/NBER/OSF/ResearchGate/RePEc URLs -- check `requests.source_kind` first.
- Twitter/X URLs use a separate two-tier extractor (`app/adapters/twitter/`), not the main chain.
- Authorization headers are redacted before persistence; debug payloads only when `DEBUG_PAYLOADS=1`.
