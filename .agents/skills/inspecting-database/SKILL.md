---
name: inspecting-database
description: Query and inspect the Ratatoskr Postgres database for debugging requests, summaries, crawl results, and LLM calls. Trigger keywords -- correlation IDs, request status, API costs, crawl results, missing summaries, database query, Postgres, debug request.
version: 3.0.0
allowed-tools: Bash, Read
---

# Database Inspection Skill

Helps query and inspect the Ratatoskr Postgres database for debugging and analysis.

## Connecting

Ratatoskr uses **PostgreSQL 16 via SQLAlchemy 2.0 + asyncpg**. The default
deployment runs Postgres in the `ratatoskr-postgres` Docker container; the
runtime DSN is read from the `DATABASE_URL` environment variable.

Use `psql` either through the container or via a local client:

```bash
# Through the deployment container (no client install required)
docker exec -it ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr

# Or against any DSN
psql "$DATABASE_URL"
```

For one-shot queries, append `-c '<sql>'` and pass `--csv` or `--json`
formatting flags as needed.

## Dynamic Context

```bash
!docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -t -c "SELECT count(*) AS total, count(*) FILTER (WHERE status = 'error') AS errors FROM requests"
```

## Common Query Patterns

### Find Request by Correlation ID

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT * FROM requests WHERE correlation_id = '<correlation_id>';"
```

### Recent Requests

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT id, type, status, input_url, created_at
     FROM requests
    ORDER BY created_at DESC
    LIMIT 10;"
```

### Failed Requests

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT id, type, status, input_url, created_at
     FROM requests
    WHERE status = 'error'
    ORDER BY created_at DESC
    LIMIT 20;"
```

### Crawl Results for a Request

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT request_id, source_url, status, firecrawl_success,
          firecrawl_error_message, http_status
     FROM crawl_results
    WHERE request_id = (
            SELECT id FROM requests WHERE correlation_id = '<correlation_id>'
          );"
```

### LLM Calls for a Request

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT id, model, status, attempt_index, attempt_trigger,
          tokens_prompt, tokens_completion, cost_usd, error_text
     FROM llm_calls
    WHERE request_id = (
            SELECT id FROM requests WHERE correlation_id = '<correlation_id>'
          )
    ORDER BY attempt_index;"
```

### Summary Output

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT request_id, lang, json_payload, version
     FROM summaries
    WHERE request_id = (
            SELECT id FROM requests WHERE correlation_id = '<correlation_id>'
          );"
```

### Telegram Message Snapshot

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT message_id, chat_id, text_full, forward_from_chat_title
     FROM telegram_messages
    WHERE request_id = (
            SELECT id FROM requests WHERE correlation_id = '<correlation_id>'
          );"
```

### List Tables / Show Schema

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "\dt"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "\d requests"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "\d crawl_results"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "\d llm_calls"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "\d summaries"
```

### Counts and Distributions

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT type, count(*) FROM requests GROUP BY type ORDER BY count DESC;"

docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT status, count(*) FROM requests GROUP BY status ORDER BY count DESC;"

docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT avg(tokens_prompt) AS avg_prompt,
          avg(tokens_completion) AS avg_completion
     FROM llm_calls
    WHERE status = 'ok';"

docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT sum(cost_usd) AS total_cost
     FROM llm_calls
    WHERE cost_usd IS NOT NULL;"
```

## Usage Tips

1. **Pretty-print JSON columns:**

   ```bash
   docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -At -c \
     "SELECT json_payload FROM summaries
       WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>');" \
     | python -m json.tool
   ```

2. **Search by URL pattern:**

   ```bash
   docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
     "SELECT id, input_url, status
        FROM requests
       WHERE input_url ILIKE '%example.com%';"
   ```

3. **Run multi-line scripts:** pipe a heredoc through `psql` or save to a
   file and pass `-f script.sql`.

## Important Notes

- `requests.id` is an integer surrogate; `requests.correlation_id` is the
  human-readable trace identifier returned in errors and Telegram replies.
- The `llm_calls` table stores ALL attempts (including failures);
  `attempt_index` and `attempt_trigger` are useful for distinguishing
  retries, repair loops, and stream fallbacks.
- Authorization headers are redacted before persistence.
- Full-text search runs on a Postgres `TSVECTOR` + GIN column (table:
  `topic_search_index`); the historical SQLite FTS5 surface no longer exists.

## Schema Reference

**Key tables**: `requests`, `telegram_messages`, `crawl_results`, `llm_calls`, `summaries`, `summary_embeddings`

See `app/db/models/` for the SQLAlchemy 2.0 typed declarative models, grouped by area:

| Module | Models |
| ------ | ------ |
| `core.py` | `User`, `Request`, `TelegramMessage`, `CrawlResult`, `LLMCall`, `Summary`, `SummaryEmbedding`, `VideoDownload`, `AudioGeneration`, ... |
| `aggregation.py` | `AggregationSession`, `AggregationSessionItem` |
| `batch.py` | `BatchSession`, `BatchSessionItem` |
| `collections.py` | `Collection`, `CollectionItem`, `CollectionCollaborator`, `CollectionInvite` |
| `digest.py` | `Channel`, `ChannelSubscription`, `ChannelPost`, `ChannelPostAnalysis`, ... |
| `repository.py` | `Repository`, `RepositoryEmbedding`, `UserGitHubIntegration` |
| `rss.py` | `RSSFeed`, `RSSFeedSubscription`, `RSSFeedItem`, `RSSItemDelivery` |
| `rules.py` | `WebhookSubscription`, `AutomationRule`, `RuleExecutionLog`, ... |
| `signal.py` | `Source`, `Subscription`, `FeedItem`, `Topic`, `UserSignal` |
| `topic_search.py` | `TopicSearchIndex` (Postgres TSVECTOR + GIN) |

Migrations live in `app/db/alembic/versions/` and are applied with
`python -m app.cli.migrate_db`.
