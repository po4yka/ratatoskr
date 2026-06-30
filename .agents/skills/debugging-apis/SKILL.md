---
name: debugging-apis
description: Debug Firecrawl and OpenRouter API calls -- request/response inspection, error handling, retry logic. Trigger on "API debug", "Firecrawl errors", "OpenRouter failures", "LLM call errors", "rate limit", "API costs", "crawl failures".
version: 2.0.0
allowed-tools: Bash, Read, Grep
---

# API Debugging

Debug and troubleshoot Firecrawl (content scraping) and OpenRouter (LLM) API integrations.

## Dynamic Context

Recent LLM failures (last 24h):

!docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -t -c "SELECT count(*) FROM llm_calls WHERE status <> 'ok' AND created_at > now() - interval '24 hours'"

## Debugging Approach

1. **Get the correlation_id** from the error message or Telegram reply.
2. **Resolve to the integer request id**:
   `SELECT id FROM requests WHERE correlation_id = '<correlation_id>'`.
3. **Check DB tables** in order: `requests` → `crawl_results` → `llm_calls` → `summaries`.
4. **Inspect payloads** for the failing step.
5. **Test the external API directly** if the issue is upstream.

## Key DB Queries

### Check crawl results for a request

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT request_id, source_url, status, firecrawl_success,
          firecrawl_error_code, firecrawl_error_message, http_status, latency_ms
     FROM crawl_results
    WHERE request_id = (
            SELECT id FROM requests WHERE correlation_id = '<correlation_id>'
          );"
```

### Check LLM calls for a request

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT id, model, status, attempt_index, attempt_trigger,
          tokens_prompt, tokens_completion,
          cost_usd, latency_ms, error_text, created_at
     FROM llm_calls
    WHERE request_id = (
            SELECT id FROM requests WHERE correlation_id = '<correlation_id>'
          )
    ORDER BY attempt_index;"
```

### View full LLM request/response

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -At -c \
  "SELECT request_messages_json
     FROM llm_calls
    WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>')
    LIMIT 1;" \
  | python -m json.tool

docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -At -c \
  "SELECT response_json
     FROM llm_calls
    WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>')
    LIMIT 1;" \
  | python -m json.tool
```

## Integration Locations

| Service     | Client                                              | DB Table        |
|-------------|-----------------------------------------------------|-----------------|
| Firecrawl   | `app/adapters/content/scraper/firecrawl_provider.py` | `crawl_results` |
| OpenRouter  | `app/adapters/openrouter/openrouter_client.py`       | `llm_calls`     |

Supporting files:

- **Firecrawl parser**: `app/adapters/external/firecrawl_parser.py`
- **OpenRouter error handler**: `app/adapters/openrouter/error_handler.py`
- **OpenRouter request builder**: `app/adapters/openrouter/request_builder.py`
- **Scraper chain**: `app/adapters/content/scraper/` (protocol, chain, factory, providers)

## Enable Debug Logging

```bash
export DEBUG_PAYLOADS=1
export LOG_LEVEL=DEBUG
# Authorization headers are automatically redacted in logs and DB
```

## Important Notes

- All auth headers are stripped before DB storage
- Both request and response payloads are persisted (even on error)
- Correlation IDs tie Telegram messages -> DB requests -> logs
- Retry logic uses exponential backoff to prevent thundering herd
- Every successful LLM call logs tokens and estimated cost

## Reference Files

Detailed API specs, curl examples, error codes, and debugging scenarios:

- `references/firecrawl-api.md` -- Firecrawl endpoints, request format, error codes, curl tests
- `references/openrouter-api.md` -- OpenRouter endpoints, request format, model fallback, curl tests
- `references/debugging-scenarios.md` -- Common failure patterns: empty content, invalid JSON, rate limits, high costs
