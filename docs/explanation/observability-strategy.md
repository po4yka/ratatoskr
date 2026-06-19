# Observability Strategy

How Ratatoskr implements logging, tracing, and debugging to ensure production reliability.

**Audience:** Developers, Operators **Type:** Explanation **Related:** [Design Philosophy](design-philosophy.md), [TROUBLESHOOTING](../reference/troubleshooting.md)

---

## Core Principle

> **If you can't debug it, you don't own it.**

External API failures (Firecrawl rate limits, OpenRouter model outages, Telegram API changes) are inevitable. Observability enables self-service debugging without developer intervention.

---

## The Observability Stack

### 0. OpenTelemetry Traces (Tempo)

OpenTelemetry is opt-in with `OTEL_ENABLED=true`. The default exporter is OTLP/gRPC to `OTEL_EXPORTER_OTLP_ENDPOINT` and production compose points this at Tempo (`http://tempo:4317`); local debugging can use `OTEL_TRACES_EXPORTER=console` or `OTEL_TRACES_EXPORTER=file OTEL_FILE_EXPORTER_PATH=/data/traces/spans.jsonl`.

FastAPI, httpx, Redis, Taskiq trace-context propagation, Telethon helper spans, summarize graph nodes, scraper-chain providers, OpenRouter calls, database session/transaction boundaries, and application use cases are all part of the same trace tree. Application spans use `ratatoskr.use_case.name`, `ratatoskr.user_id`, and `ratatoskr.correlation_id` when those values are available. Database spans use `ratatoskr.db.operation`; scraper spans use `ratatoskr.scraper.*`; LLM spans use `ratatoskr.llm.*`.

Example API repository-analysis trace in Tempo:

```text
POST /v1/repositories/{repository_id}/analyze
  use_case.analyze_repository.analyze {ratatoskr.correlation_id=req_abc123, ratatoskr.repository.id=456}
    db.session {ratatoskr.db.operation=session}
    agent.repo_analysis {ratatoskr.correlation_id=req_abc123}
      llm.chat_structured {ratatoskr.llm.provider=openrouter, ratatoskr.correlation_id=req_abc123}
    db.transaction {ratatoskr.db.operation=transaction}
    embedding.encode {ratatoskr.embedding.batch_size=1}
    vector.replace {ratatoskr.vector.operation=replace}
```

Example Telegram summary trace:

```text
telegram.update {ratatoskr.correlation_id=req_abc123}
  url_flow.process {ratatoskr.correlation_id=req_abc123}
    graph.summarize.extract {ratatoskr.graph.node=extract}
      scraper.chain {ratatoskr.scraper.mode=tiered_race}
        scraper.scrapling {ratatoskr.scraper.provider=scrapling, ratatoskr.scraper.outcome=no_content}
        scraper.defuddle {ratatoskr.scraper.provider=defuddle, ratatoskr.scraper.outcome=success}
    graph.summarize.summarize {ratatoskr.graph.node=summarize}
      llm.chat {ratatoskr.llm.provider=openrouter, ratatoskr.llm.tokens_total=1894}
    graph.summarize.persist {ratatoskr.graph.node=persist}
      db.transaction {ratatoskr.db.operation=transaction}
```

In Grafana, add a Tempo panel that filters by `resource.service.name="ratatoskr"` and exposes `ratatoskr.correlation_id` and `ratatoskr.user_id` as searchable span attributes. The fastest incident workflow is: copy the user-visible Error ID, search Tempo by `ratatoskr.correlation_id`, then pivot from the slowest scraper/LLM/database span into Loki using the same correlation ID.

### 1. Correlation IDs (Request Tracing)

**Problem:** When a user reports "my summary failed", how do you trace the request across Telegram → Database → Firecrawl → OpenRouter → Logs?

**Solution:** Every request gets a unique `correlation_id` that flows through all systems.

**Flow:**

```
Telegram Message (message.id = 12345)
  ↓
Request (id = "req_abc123", telegram_message_id = 12345)
  ↓
CrawlResult (request_id = "req_abc123")
  ↓
LLMCall (request_id = "req_abc123")
  ↓
Summary (request_id = "req_abc123")
  ↓
Logs (correlation_id = "req_abc123")
```

**Usage:**

```bash
# User reports error: "Error ID: req_abc123"

# Trace full request lifecycle (Postgres via the deployment container)
PSQL="docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c"
$PSQL "SELECT * FROM requests        WHERE correlation_id = 'req_abc123';"
$PSQL "SELECT * FROM crawl_results   WHERE request_id = (SELECT id FROM requests WHERE correlation_id = 'req_abc123');"
$PSQL "SELECT * FROM llm_calls       WHERE request_id = (SELECT id FROM requests WHERE correlation_id = 'req_abc123') ORDER BY attempt_index;"
$PSQL "SELECT * FROM summaries       WHERE request_id = (SELECT id FROM requests WHERE correlation_id = 'req_abc123');"

# Search logs
grep "req_abc123" logs/bot.log
```

**Benefit:** End-to-end tracing without distributed tracing infrastructure.

---

### 2. Structured Logging (Machine-Readable Logs)

**Library:** Loguru (structured JSON logs)

**Configuration:**

```python
# app/core/logging_utils.py
logger.configure(
    handlers=[{
        "sink": sys.stdout,
        "format": "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        "serialize": False,  # Human-readable by default
    }]
)

# Enable JSON for production
if os.getenv("LOG_FORMAT") == "json":
    logger.configure(handlers=[{"sink": sys.stdout, "serialize": True}])
```

**Example Log Entry (JSON mode):**

```json
{
  "timestamp": "2026-02-09T14:32:15.123456Z",
  "level": "INFO",
  "logger": "app.adapters.telegram.message_router",
  "function": "route_message",
  "line": 45,
  "message": "Routing URL message",
  "extra": {
    "correlation_id": "req_abc123",
    "user_id": 123456789,
    "url": "https://example.com/article",
    "dedupe_hash": "a1b2c3d4..."
  }
}
```

**Benefit:** Parse logs with `jq`, aggregate in log management systems (CloudWatch, Loki, etc.).

---

### 3. Full Payload Persistence (Audit Trail)

**Problem:** LLM output is non-deterministic. If a summary is incorrect, how do you debug without the exact prompt and response?

**Solution:** Persist everything in PostgreSQL.

**Stored Data:**

- **Telegram Messages:** Full message JSON (in `telegram_messages.message_snapshot`)
- **Firecrawl Responses:** Full crawl result (in `crawl_results.crawl_result_json`)
- **OpenRouter Requests/Responses:** Prompt, completion, token counts (in `llm_calls`)
- **Summaries:** Final validated JSON (in `summaries.summary_json`)

**Example:**

```python
# app/adapters/openrouter/openrouter_client.py
async def complete(self, prompt: str) -> str:
    response = await self.http_client.post("/chat/completions", json={...})

    # Persist request and response
    LLMCall.create(
        request_id=correlation_id,
        model=self.model,
        prompt=prompt,  # Full prompt stored
        completion=response["choices"][0]["message"]["content"],  # Full response stored
        tokens_used=response["usage"]["total_tokens"],
        # ...
    )

    return response["choices"][0]["message"]["content"]
```

**Benefit:** Reproduce failures exactly, audit LLM behavior, analyze prompt engineering changes.

---

### 4. Debug Mode (Payload Inspection)

**Environment Variable:** `DEBUG_PAYLOADS=1`

**Behavior:** Log previews of external API requests/responses (with sensitive data redacted).

**Example:**

```python
# app/adapters/content/content_extractor.py
if os.getenv("DEBUG_PAYLOADS") == "1":
    logger.debug(
        "Firecrawl request preview",
        extra={
            "url": url,
            "headers": {k: v for k, v in headers.items() if k.lower() != "authorization"},  # Redact API key
            "body_preview": json.dumps(payload)[:500],  # First 500 chars
        }
    )
```

**Benefit:** Debug API failures without exposing secrets in logs.

---

### 5. Error Enrichment (Contextual Error Messages)

**Pattern:** All user-facing errors include `Error ID: <correlation_id>` for tracing.

**Implementation:**

```python
# app/adapters/telegram/response_formatter.py
async def send_error_message(self, chat_id: int, error: Exception, correlation_id: str):
    user_message = f"An error occurred processing your request.\n\n"
    user_message += f"Error ID: {correlation_id}\n\n"
    user_message += "Please include this ID if reporting the issue."

    await self.bot.send_message(chat_id, user_message)

    # Log full error details with correlation_id
    logger.error(
        "Error processing request",
        extra={
            "correlation_id": correlation_id,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "traceback": traceback.format_exc(),
        }
    )
```

**Benefit:** Users can self-service debug by searching logs/database with correlation ID.

---

### 6. Exception Visibility Policy (No Silent Suppression)

**Rule:** Avoid `except: pass` in production paths.

When exceptions are intentionally swallowed (for resilience/fallback behavior), emit at least a debug-level event with enough context to diagnose the fallback path.

**Preferred pattern:**

```python
try:
    optional_operation()
except KnownError as exc:
    logger.debug("optional_operation_failed", extra={"error": str(exc), "context": context_id})
    # continue with fallback
```

**Why:** This preserves user-facing resilience while preventing invisible failures that block root-cause analysis.

---

### 7. Sensitive Logging Guardrails

Do not log raw credentials, bearer tokens, API keys, refresh tokens, or full token hashes.

Use one of:

- Omit sensitive fields entirely.
- Log an irreversible short fingerprint only when required for diagnostics.
- Prefer neutral wording in messages and CLI help (e.g., “credential” rather than key/token examples) when examples could encourage unsafe copy/paste into logs.

**Review checklist for log changes:**

1. Could this message leak a secret if copied to support tickets?
2. Could this `extra` payload include auth headers or token-like fields?
3. If this is a fallback path, is there enough context to debug without exposing sensitive values?

Social OAuth and content-fetch paths are covered by the same guardrail. Redaction includes access tokens, refresh tokens, authorization codes, OAuth state values, cookies, `Authorization` headers, and token-bearing callback URLs. Social auth/fetch logs should carry `cid` for correlation and provider/status context, but not decrypted token material or raw provider payloads.

### Social Integration Metrics

Connected-account social workflows expose four Prometheus counters:

| Metric | Labels | Meaning |
| ------ | ------ | ------- |
| `ratatoskr_social_fetch_total` | `provider`, `status`, `auth_tier` | Content fetch attempts recorded through `SocialFetchAttempt` |
| `ratatoskr_social_token_refresh_total` | `provider`, `status` | OAuth token refresh outcomes |
| `ratatoskr_social_rate_limit_total` | `provider` | Provider rate-limit responses, including authenticated feed ingestors |
| `ratatoskr_social_connection_status_total` | `provider`, `status` | Observed connection states from list/upsert/update/delete paths |

The fetch counter is intentionally emitted at the persistence boundary in `SocialConnectionRepositoryAdapter.record_fetch_attempt()`. This makes the metric provider-neutral and keeps adapters from duplicating status/auth-tier labeling logic. If `prometheus_client` is not installed, these recorder functions no-op like the rest of `app/observability/metrics.py`.

### Aggregation API Audit Events

External aggregation creation now emits explicit audit events so operators can correlate expensive bundle creation with the authenticated actor that triggered it.

- `aggregation.bundle_create_requested`
- `aggregation.bundle_create_blocked_ssrf`
- `aggregation.bundle_create_succeeded`
- `aggregation.bundle_create_failed`

Each event includes the request `correlation_id`, authenticated `user_id`, `client_id`, submitted bundle size, and language preference. Blocked-SSRF events also include the rejected URL, item position, and block reason. Success events also include the persisted aggregation `session_id` and outcome counts.

---

## Tracing External API Calls

### Firecrawl API Tracing

**Stored Fields:**

- `crawl_results.url_crawled` - URL sent to Firecrawl
- `crawl_results.crawl_result_json` - Full Firecrawl response
- `crawl_results.tokens_used` - Tokens consumed
- `crawl_results.error_message` - Error if crawl failed

**Debugging:**

```sql
-- Find failed Firecrawl calls
SELECT url_crawled, error_message, created_at
FROM crawl_results
WHERE error_message IS NOT NULL
ORDER BY created_at DESC
LIMIT 10;

-- Analyze token usage
SELECT AVG(tokens_used), MAX(tokens_used)
FROM crawl_results
WHERE created_at > now() - interval '7 days';
```

---

### OpenRouter API Tracing

**Stored Fields:**

- `llm_calls.model` - Model used (e.g., `deepseek/deepseek-v4-flash`)
- `llm_calls.prompt` - Full prompt sent to LLM
- `llm_calls.completion` - Full LLM response
- `llm_calls.prompt_tokens` - Input tokens
- `llm_calls.completion_tokens` - Output tokens
- `llm_calls.total_tokens` - Sum
- `llm_calls.error_message` - Error if LLM call failed

**Debugging:**

```sql
-- Find failed LLM calls
SELECT model, error_message, created_at
FROM llm_calls
WHERE error_message IS NOT NULL
ORDER BY created_at DESC
LIMIT 10;

-- Analyze token costs
SELECT
  model,
  COUNT(*) as calls,
  SUM(total_tokens) as total_tokens,
  AVG(total_tokens) as avg_tokens_per_call
FROM llm_calls
WHERE created_at > now() - interval '30 days'
GROUP BY model
ORDER BY total_tokens DESC;
```

---

### Telegram API Tracing

**Stored Fields:**

- `telegram_messages.message_snapshot` - Full Telegram message JSON
- `telegram_messages.user_id` - Sender ID
- `telegram_messages.chat_id` - Chat ID
- `telegram_messages.message_id` - Telegram message ID

**Debugging:**

```sql
-- Find recent user messages
SELECT message_snapshot, created_at
FROM telegram_messages
WHERE user_id = 123456789
ORDER BY created_at DESC
LIMIT 10;

-- Trace message to request
SELECT tm.message_id, r.id as request_id, r.url
FROM telegram_messages tm
JOIN requests r ON r.telegram_message_id = tm.id
WHERE tm.message_id = 12345;
```

---

## Performance Monitoring

### Database Metrics

**Query Performance:**

```sql
-- Average processing time per request
SELECT ROUND(AVG(total_processing_time_sec), 2) as avg_sec
FROM requests
WHERE created_at > now() - interval '7 days';

-- Slow requests (>15 seconds)
SELECT url, total_processing_time_sec, created_at
FROM requests
WHERE total_processing_time_sec > 15
ORDER BY total_processing_time_sec DESC
LIMIT 10;
```

**Storage Growth:**

```sql
-- Database size growth
SELECT
  DATE(created_at) as date,
  COUNT(*) as summaries_created,
  SUM(LENGTH(summary_json)) as total_json_bytes
FROM summaries
GROUP BY DATE(created_at)
ORDER BY date DESC
LIMIT 30;
```

---

### API Cost Monitoring

**Token Usage:**

```sql
-- Token usage by model (last 30 days)
SELECT
  model,
  COUNT(*) as calls,
  SUM(prompt_tokens) as total_prompt_tokens,
  SUM(completion_tokens) as total_completion_tokens,
  SUM(total_tokens) as total_tokens
FROM llm_calls
WHERE created_at > now() - interval '30 days'
GROUP BY model
ORDER BY total_tokens DESC;
```

**Estimated Costs:**

```python
# app/cli/analyze_costs.py
def estimate_costs():
    """Estimate API costs based on token usage."""
    llm_calls = db.llm_calls.select().where(
        LLMCall.created_at > datetime.now() - timedelta(days=30)
    )

    costs = {}
    for call in llm_calls:
        model_pricing = {
            "deepseek/deepseek-v4-flash": {"prompt": 0.14, "completion": 0.28},  # per 1M tokens
            "qwen/qwen3-max": {"prompt": 0.20, "completion": 0.60},
            # ...
        }

        pricing = model_pricing.get(call.model, {"prompt": 0, "completion": 0})
        cost = (call.prompt_tokens * pricing["prompt"] + call.completion_tokens * pricing["completion"]) / 1_000_000

        costs[call.model] = costs.get(call.model, 0) + cost

    return costs
```

---

## Error Tracking

### Error Categories

**1. Configuration Errors** (fail-fast at startup)

- Missing required env vars (`FIRECRAWL_API_KEY`, `OPENROUTER_API_KEY`)
- Invalid env var values (`MAX_CONCURRENT_CALLS = "abc"`)

**2. External API Errors** (graceful degradation)

- Firecrawl rate limits (429) → wait and retry
- OpenRouter model outage (503) → fallback to secondary model
- Redis connection failure → disable cache, continue without it

**3. User Input Errors** (immediate feedback)

- Invalid URL format → reply with error message
- Unsupported content type → reply with "Cannot summarize X"

**4. LLM Output Errors** (self-correction)

- Invalid JSON → retry with error feedback (up to 3x)
- Missing required fields → validation backfills defaults
- Character limit violations → truncate with warning

---

### Error Logging Pattern

**Structured Error Logs:**

```python
try:
    summary = await summarize_content(content, correlation_id)
except ValidationError as e:
    logger.error(
        "Summary validation failed",
        extra={
            "correlation_id": correlation_id,
            "error_type": "ValidationError",
            "error_details": e.errors(),
            "raw_summary_preview": summary_json[:500],
        }
    )
    raise
```

**Benefit:** Aggregate errors by type, identify patterns (e.g., "90% of validation errors are from missing `key_ideas` field").

---

## Debugging Workflows

### Workflow 1: User Reports "Summary Failed"

**Step 1:** Get correlation ID from error message

```
User message: "Error ID: req_abc123"
```

**Step 2:** Query database for request

```sql
SELECT * FROM requests WHERE id = 'req_abc123';
```

**Step 3:** Check external API calls

```sql
-- Firecrawl result
SELECT error_message, crawl_result_json FROM crawl_results WHERE request_id = 'req_abc123';

-- LLM calls
SELECT model, error_message, completion FROM llm_calls WHERE request_id = 'req_abc123';
```

**Step 4:** Search logs for detailed error

```bash
grep "req_abc123" logs/bot.log | grep ERROR
```

**Outcome:** Identify root cause (Firecrawl timeout, LLM rate limit, validation failure, etc.).

---

### Workflow 2: "LLM Costs Are High"

**Step 1:** Analyze token usage by model

```sql
SELECT
  model,
  COUNT(*) as calls,
  SUM(total_tokens) as total_tokens,
  ROUND(AVG(total_tokens), 2) as avg_tokens
FROM llm_calls
WHERE created_at > now() - interval '30 days'
GROUP BY model
ORDER BY total_tokens DESC;
```

**Step 2:** Identify expensive requests

```sql
SELECT
  r.url,
  l.model,
  l.total_tokens,
  l.created_at
FROM llm_calls l
JOIN requests r ON l.request_id = r.id
WHERE l.total_tokens > 50000
ORDER BY l.total_tokens DESC
LIMIT 10;
```

**Step 3:** Optimize

- Switch to cheaper model (DeepSeek V3 → Qwen 3 Max)
- Enable Redis caching (`REDIS_ENABLED=true`)
- Reduce content length (`MAX_CONTENT_LENGTH_TOKENS=30000`)

---

### Workflow 3: "Summary Quality Is Poor"

**Step 1:** Check quality scores

```sql
SELECT
  url,
  JSON_EXTRACT(summary_json, '$.quality_scores.accuracy') as accuracy,
  JSON_EXTRACT(summary_json, '$.quality_scores.coherence') as coherence,
  JSON_EXTRACT(summary_json, '$.hallucination_risk.level') as hallucination_risk
FROM summaries
WHERE created_at > now() - interval '7 days'
ORDER BY accuracy ASC
LIMIT 10;
```

**Step 2:** Inspect failing summaries

```sql
SELECT summary_json FROM summaries WHERE id = 'summary_xyz';
```

**Step 3:** Review LLM prompt and response

```sql
SELECT prompt, completion FROM llm_calls WHERE request_id = 'req_abc123';
```

**Outcome:** Identify prompt engineering issues, switch to better model, or flag content as unsummarizable.

---

## Telemetry and Metrics

### Metrics Collected

**Request Metrics:**

- Total requests per day
- Success rate (summaries created / requests)
- Average processing time
- P50, P95, P99 latency

**API Metrics:**

- Firecrawl calls per day, success rate, average tokens
- OpenRouter calls per day, success rate, average tokens, costs
- Telegram messages sent per day

**Quality Metrics:**

- Average confidence scores
- Hallucination risk distribution (low/medium/high)
- Readability scores distribution

---

### Metrics Queries

**Daily Summary:**

```sql
SELECT
  DATE(created_at) as date,
  COUNT(*) as total_requests,
  SUM(CASE WHEN error_message IS NULL THEN 1 ELSE 0 END) as successful_requests,
  ROUND(AVG(total_processing_time_sec), 2) as avg_processing_time_sec
FROM requests
GROUP BY DATE(created_at)
ORDER BY date DESC
LIMIT 30;
```

**Model Performance:**

```sql
SELECT
  model,
  COUNT(*) as total_calls,
  SUM(CASE WHEN error_message IS NULL THEN 1 ELSE 0 END) as successful_calls,
  ROUND(100.0 * SUM(CASE WHEN error_message IS NULL THEN 1 ELSE 0 END) / COUNT(*), 2) as success_rate_pct
FROM llm_calls
WHERE created_at > now() - interval '7 days'
GROUP BY model
ORDER BY total_calls DESC;
```

---

## Log Management

### Log Levels

**DEBUG:** Detailed payload previews (requires `DEBUG_PAYLOADS=1`)

- Firecrawl request/response previews
- OpenRouter request/response previews
- Validation details

**INFO:** Normal operations

- Request started
- Summary created
- API calls succeeded

**WARNING:** Degraded operations

- Redis unavailable (continuing without cache)
- Fallback model used
- Validation backfilled missing fields

**ERROR:** Failed operations

- Firecrawl API error
- OpenRouter rate limit exceeded
- Summary validation failed after 3 retries

---

### Log Rotation

**Production Setup:**

```bash
# Use logrotate for file-based logs
# /etc/logrotate.d/ratatoskr
/var/log/ratatoskr/*.log {
    daily
    rotate 30
    compress
    delaycompress
    notifempty
    create 0644 ratatoskr ratatoskr
    sharedscripts
    postrotate
        systemctl reload ratatoskr
    endscript
}
```

---

### Centralized Logging (Optional)

**Loki + Grafana:**

```yaml
# ops/docker/docker-compose.yml
services:
  loki:
    image: grafana/loki:latest
    ports:
      - "3100:3100"

  grafana:
    image: grafana/grafana:latest
    ports:
      - "3000:3000"
    environment:
      - GF_AUTH_ANONYMOUS_ENABLED=true

  promtail:
    image: grafana/promtail:latest
    volumes:
      - /var/log/ratatoskr:/var/log/ratatoskr
    command: -config.file=/etc/promtail/config.yml
```

**Benefit:** Query logs with LogQL, visualize trends, set up alerts.

---

## Alerting

### Alert Conditions

**High Priority:**

- Error rate >10% (last 1 hour)
- No successful requests in last 2 hours
- Database corruption detected

**Medium Priority:**

- Average processing time >20s (last 1 hour)
- Firecrawl success rate <80% (last 1 hour)
- Token costs >$10/day

**Low Priority:**

- Redis unavailable (graceful degradation)
- Disk space >80% used

---

### Alert Routing

Prometheus and Loki both route alerts to Alertmanager at `http://alertmanager:9093`. Prometheus loads `ops/monitoring/alerting_rules.yml`; Loki's ruler uses the same Alertmanager URL from `ops/monitoring/loki-config.yml`.

Run the monitoring stack with a real receiver URL:

```bash
export GRAFANA_ADMIN_PASSWORD='<strong-password>'
export ALERT_WEBHOOK_URL='https://example.internal/ratatoskr/alerts'
docker compose -f ops/docker/docker-compose.monitoring.yml up -d
```

The primary compose stack also includes Alertmanager behind the `with-monitoring` profile:

```bash
ALERT_WEBHOOK_URL='https://example.internal/ratatoskr/alerts' docker compose -f ops/docker/docker-compose.yml --profile with-monitoring up -d alertmanager prometheus loki
```

`ops/monitoring/alertmanager.yml` defines the default webhook receiver and includes commented examples for Slack, a Telegram bot webhook, and PagerDuty. Compose supplies a local discard URL when `ALERT_WEBHOOK_URL` is unset so development stacks still boot. In production (`RATATOSKR_ENV=production`), the Alertmanager container logs an error on startup unless at least one receiver variable is configured.

Manual smoke test:

```bash
curl -sS http://127.0.0.1:9090/-/healthy
curl -sS http://127.0.0.1:9093/-/ready
curl -sS -H 'Content-Type: application/json' -d '[{"labels":{"alertname":"RatatoskrManualSmoke","severity":"warning"},"annotations":{"summary":"manual Alertmanager smoke test"}}]' http://127.0.0.1:9093/api/v2/alerts
```

---

## Normalized Extraction Failure Snapshots

Failed URL/article processing now writes a normalized snapshot to `requests.error_context_json`. This is the fastest way to identify stage, component, reason code, and retryability without log scraping.

```sql
SELECT
  id,
  status,
  error_type,
  error_message,
  json_extract(error_context_json, '$.stage')        AS stage,
  json_extract(error_context_json, '$.component')    AS component,
  json_extract(error_context_json, '$.reason_code')  AS reason_code,
  json_extract(error_context_json, '$.retryable')    AS retryable
FROM requests
WHERE status = 'error'
ORDER BY updated_at DESC
LIMIT 20;
```

Prometheus metrics for extraction failure analysis:

- `ratatoskr_extraction_failures_total{stage,component,reason_code,retryable}`
- `ratatoskr_extraction_attempts_total{stage,component,outcome}`
- `ratatoskr_extraction_stage_latency_seconds{stage,component,outcome}`

---

## Best Practices

### 1. Always Include Correlation IDs

**Pattern:** Pass `correlation_id` to all functions, include in all logs.

```python
async def summarize_content(content: str, correlation_id: str) -> dict:
    logger.info("Starting summarization", extra={"correlation_id": correlation_id})
    # ...
```

---

### 2. Redact Sensitive Data

**Pattern:** Strip `Authorization` headers, API keys, user tokens before logging.

```python
safe_headers = {k: v for k, v in headers.items() if k.lower() not in ["authorization", "x-api-key"]}
logger.debug("HTTP request", extra={"headers": safe_headers})
```

---

### 3. Log Exceptions with Context

**Pattern:** Use `logger.exception()` to capture full traceback.

```python
try:
    result = await external_api_call()
except Exception as e:
    logger.exception("External API failed", extra={"correlation_id": correlation_id, "url": url})
    raise
```

---

### 4. Use Structured Logging

**Good:**

```python
logger.info("Summary created", extra={"correlation_id": req_id, "url": url, "tokens": tokens})
```

**Bad:**

```python
logger.info(f"Summary created for {url} using {tokens} tokens (correlation_id: {req_id})")
```

**Benefit:** Structured logs are parseable with `jq`, filterable by field.

---

## See Also

- [Design Philosophy](design-philosophy.md) - Overall architectural principles
- [TROUBLESHOOTING](../reference/troubleshooting.md) - Common debugging scenarios
- [How to Optimize Performance](../guides/optimize-performance.md) - Performance tuning
- [FAQ § Debugging](faq.md#debugging) - Common debugging questions

---

**Last Updated:** 2026-02-09
