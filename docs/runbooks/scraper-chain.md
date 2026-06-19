# Scraper Chain Runbook

Use this when URL extraction stalls, a provider becomes degraded, scraper-chain attempts exhaust, or users report summaries failing before the LLM step.

## Symptoms

- Alert `RatatoskrFirecrawlHighErrors`, `RatatoskrFirecrawlNoRequests`, `RatatoskrFirecrawlHighLatency`, `RatatoskrHighLatency`, or `RatatoskrHighErrorRate` fires.
- `Ratatoskr — Scraper Chain` shows one provider with low success rate, high latency, or a sudden attempt-rate spike.
- User-visible error says extraction failed or includes reason `SCRAPER_CHAIN_EXHAUSTED`.
- Logs contain `scraper_chain_exhausted`, `scraper_attempt`, `provider_failed`, `crawl4ai`, `firecrawl`, `defuddle`, `cloakbrowser`, `playwright`, `crawlee`, or `direct_html`.
- `crawl_results` has repeated non-`ok` rows for the same host or provider.

## Log Queries

```bash
docker compose -f ops/docker/docker-compose.yml logs --tail=400 ratatoskr worker | rg 'scraper|crawl4ai|firecrawl|defuddle|cloakbrowser|playwright|crawlee|direct_html|SCRAPER_CHAIN_EXHAUSTED'
docker compose -f ops/docker/docker-compose.yml --profile with-scrapers ps
docker compose -f ops/docker/docker-compose.yml --profile with-scrapers logs --tail=200 crawl4ai firecrawl-api defuddle-api cloakbrowser
```

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT scraper_used, status, count(*) AS attempts, round(avg(latency_ms)) AS avg_ms FROM crawl_results WHERE created_at > now() - interval '2 hours' GROUP BY scraper_used, status ORDER BY attempts DESC;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT r.correlation_id, r.input_url, cr.scraper_used, cr.status, cr.http_status, cr.latency_ms, left(cr.firecrawl_error_message, 160) AS err FROM crawl_results cr JOIN requests r ON r.id = cr.request_id WHERE cr.created_at > now() - interval '2 hours' ORDER BY cr.created_at DESC LIMIT 30;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -x -c "SELECT scraper_used, status, http_status, latency_ms, length(content_markdown) AS md_len, options_json FROM crawl_results WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>') ORDER BY created_at;"
```

## Prometheus Panels

- Alerts: `RatatoskrFirecrawlHighErrors`, `RatatoskrFirecrawlNoRequests`, `RatatoskrFirecrawlHighLatency`, `RatatoskrHighLatency`, `RatatoskrHighErrorRate`.
- Grafana: `Ratatoskr — Scraper Chain` (`ratatoskr-scraper-chain`) panels `Success rate by provider (5m)`, `P95 latency by provider (5m)`, and `Attempt rate by provider × status (5m)`.
- Grafana fallback: `Ratatoskr Overview` (`ratatoskr-overview`) panels `Firecrawl Requests`, `Firecrawl Latency`, `Firecrawl Circuit Breaker`, `Request Latency Percentiles`, and `Error Rate (5m)`.

## Mitigation Steps

1. Identify whether the failure is provider-specific or host-specific by grouping recent `crawl_results` by `scraper_used`, `status`, and affected host.
2. If a sidecar is down, restart only the sidecar first: `docker compose -f ops/docker/docker-compose.yml --profile with-scrapers restart crawl4ai firecrawl-api defuddle-api cloakbrowser`.
3. If one provider is slow or returning bad content, force-skip it by setting the matching `SCRAPER_<PROVIDER>_ENABLED=false` in the deployment env or by removing it from `scraper.provider_order` in `ratatoskr.yaml`, then restart `ratatoskr` and `worker`.
4. If Firecrawl-specific alerts fire but the chain still succeeds through other providers, keep Firecrawl disabled until the self-hosted stack is healthy; cloud Firecrawl is not the article-scraper fallback.
5. If browser providers OOM or hang, restart `ratatoskr`/`worker` plus the browser sidecars, then lower concurrency before re-enabling the failing provider.
6. If all providers fail for one domain but other URLs work, add a temporary host-specific route to prefer the working provider or use Webwright only if the host is explicitly allowed and cost is acceptable.
7. Reprocess one failed URL only after the provider is healthy; use the original correlation ID to compare the old attempt chain with the new `crawl_results` rows.

## Escalation

Page the maintainer if `SCRAPER_CHAIN_EXHAUSTED` affects multiple unrelated hosts for more than 15 minutes, all browser-capable providers fail after restart, a suspected SSRF/filtering bypass appears in logs, or disabling the degraded provider would remove the only working path for a critical source.

## References

- `docs/explanation/scraper-chain.md`
- `docs/reference/troubleshooting.md#content-extraction-failures`
- `.codex/skills/scraper-chain-debugging/SKILL.md`
- `app/adapters/content/scraper/`
