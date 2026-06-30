# Firecrawl API Reference

## Endpoints

- **Base URL**: `https://api.firecrawl.dev`
- **Scrape endpoint**: `POST /v2/scrape`

## Official Documentation

- **Features**: https://docs.firecrawl.dev/features/scrape
- **API Reference**: https://docs.firecrawl.dev/api-reference/endpoint/scrape
- **Advanced Guide**: https://docs.firecrawl.dev/advanced-scraping-guide

## Integration Location

- **Client**: `app/adapters/content/scraper/firecrawl_provider.py`
- **Parser**: `app/adapters/external/firecrawl_parser.py`
- **DB Storage**: `crawl_results` table

## Common Request Format

```json
{
  "url": "https://example.com/article",
  "formats": ["markdown", "html"],
  "mobile": false,
  "parsers": ["pdf"],
  "timeout": 30000
}
```

## Debugging Failed Crawls

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr <<'EOF'
SELECT
  request_id,
  source_url,
  status,
  firecrawl_success,
  firecrawl_error_code,
  firecrawl_error_message,
  http_status,
  latency_ms
FROM crawl_results
WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>');
EOF
```

## Common Error Codes

- **400**: Invalid request (bad URL, malformed params)
- **401**: Invalid API key
- **402**: Payment required (quota exceeded)
- **429**: Rate limit exceeded
- **500/502/503**: Firecrawl server errors (retry with backoff)
- **timeout**: Request exceeded timeout limit

## Retry Logic

Check scraper provider/chain in `app/adapters/content/scraper/`:

- 3 retries with exponential backoff on 5xx/timeout
- Toggle `mobile` emulation on PDF failures
- Check `parsers` configuration

## Enable Debug Logging

```bash
export DEBUG_PAYLOADS=1
export LOG_LEVEL=DEBUG
# Request/response previews logged with Authorization redacted
```

## Test Firecrawl Directly

```bash
curl -X POST https://api.firecrawl.dev/v2/scrape \
  -H "Authorization: Bearer $FIRECRAWL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "formats": ["markdown"]
  }' | python -m json.tool
```
