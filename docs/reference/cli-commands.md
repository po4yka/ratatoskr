# CLI Commands Reference

Complete reference for all command-line tools in Ratatoskr.

**Audience:** Developers, Operators **Type:** Reference **Related:** [How-To Guides](../guides/), [TROUBLESHOOTING](troubleshooting.md)

---

## Overview

Ratatoskr provides CLI tools for:

- External API access via the packaged `ratatoskr` client
- Testing summarization without Telegram (`summary.py`)
- Database migrations (`migrate_db.py`)
- Search functionality testing (`search.py`)
- Embedding and vector store management (`backfill_embeddings.py`, `backfill_vector_store.py`)
- Signal-scoring eval export and precision checks (`signal_eval.py`)
- MCP server (`mcp_server.py`)

**Common Pattern:** `python -m app.cli.<command> [options]`

## External `ratatoskr` CLI

The packaged external client lives under `clients/cli/` and talks to the public `/v1/*` API instead of the internal `app.cli.*` harnesses documented below.

Use it when you need authenticated external access, especially for mixed-source aggregation:

```bash
# Authenticate with a client secret
ratatoskr login --server https://ratatoskr.example.com --user-id 123456 --client-id cli-workstation-v1

# Submit a bundle
ratatoskr aggregate https://x.com/example/status/1 https://youtu.be/dQw4w9WgXcQ

# Reopen one session
ratatoskr aggregation get 42

# List recent sessions
ratatoskr aggregation list --limit 10

# Script with JSON
ratatoskr --json aggregate --file sources.txt | jq '.session'
```

Aggregation notes:

- `ratatoskr aggregate` accepts positional URLs, `--file`, `--lang`, and repeatable `--hint`.
- The command is currently blocking: it waits for extraction plus synthesis and returns a terminal session snapshot on success.
- Use `ratatoskr aggregation get <id>` or `ratatoskr aggregation list` to revisit persisted sessions after a network interruption or for scripting against prior runs.

See:

- [CLI README](../../clients/cli/README.md)
- [External Access Quickstart](../guides/external-access-quickstart.md)
- [Mobile API Spec](mobile-api.md)

---

## Signal Eval

**Command:** `python -m app.cli.signal_eval`

**Purpose:** Export ranked signal candidates for manual labeling and compute precision@5.

### Export Eval Set

```bash
python -m app.cli.signal_eval export \
  --dsn "$DATABASE_URL" \
  --user-id 123456 \
  --limit 100 \
  --output data/signal_eval.jsonl
```

The export is JSONL. Each row includes `rank`, `signal_id`, `status`, `final_score`, title, URL, source, topic, and a default `relevant` value derived from feedback status. Edit `relevant` manually when building a self-curated eval set.

### Precision@5

```bash
python -m app.cli.signal_eval precision --input data/signal_eval.jsonl --k 5
```

Output:

```json
{"k": 5, "evaluated": 5, "relevant": 3, "precision": 0.6}
```

For Phase 3, treat `liked` and `queued` as relevant by default. Manual `relevant` labels in the JSONL file override status-derived relevance.

### Real-Use Eval Workflow

Run a weekly export during 2-3 weeks of normal source use, label the top candidates, and keep each JSONL file with the date in the filename. Compute `precision --k 5` per file and compare the trend before changing weights, prompts, or source mix. Do not mix unlabeled rows into the precision calculation; either remove them from JSONL or set `relevant` explicitly.

---

## Summary Runner

**Command:** `python -m app.cli.summary`

**Purpose:** Test URL summarization without Telegram bot.

### Basic Usage

```bash
# Summarize single URL
python -m app.cli.summary --url https://example.com/article

# Multiple URLs (interactive mode)
python -m app.cli.summary --accept-multiple
```

### Options

| Option | Type | Default | Description |
| -------- | ------ | --------- | ------------- |
| `--url` | string | - | URL to summarize (required if not using `--accept-multiple`) |
| `--accept-multiple` | flag | false | Interactive mode: accept multiple URLs |
| `--json-path` | string | - | Save summary JSON to file |
| `--log-level` | string | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `--lang` | string | auto | Force language (en, ru, or auto-detect) |
| `--skip-web-search` | flag | false | Disable web search enrichment |
| `--output-format` | string | pretty | Output format (pretty, json, minimal) |

### Examples

**Basic Summarization:**

```bash
python -m app.cli.summary --url https://techcrunch.com/2026/01/15/ai-breakthrough/
```

**Debug Mode with JSON Output:**

```bash
python -m app.cli.summary \
  --url https://example.com/article \
  --log-level DEBUG \
  --json-path output.json
```

**Multiple URLs (Interactive):**

```bash
python -m app.cli.summary --accept-multiple
# Enter URLs one per line, Ctrl+D to finish
https://example.com/article1
https://example.com/article2
```

**Force Language:**

```bash
python -m app.cli.summary \
  --url https://habr.com/ru/post/123456/ \
  --lang ru
```

### Output

**Pretty Format (default):**

```
=== Summary for: https://example.com/article ===

Summary (250 chars):
  [summary text...]

Summary (1000 chars):
  [detailed summary...]

TL;DR:
  [one-sentence takeaway...]

Key Ideas:
  - [idea 1]
  - [idea 2]
  ...

[full summary JSON follows...]
```

**JSON Format:**

```bash
python -m app.cli.summary --url https://example.com --output-format json | jq .
```

**Minimal Format:**

```bash
python -m app.cli.summary --url https://example.com --output-format minimal
# Only prints TL;DR and key ideas
```

### Exit Codes

- `0` - Success
- `1` - Validation error (invalid URL, missing env vars)
- `2` - Content extraction failed
- `3` - LLM summarization failed
- `4` - Summary validation failed

---

## Database Migration

**Command:** `python -m app.cli.migrate_db`

**Purpose:** Inspect and apply database migrations.

### Basic Usage

```bash
# Render pending migrations as SQL without applying them
python -m app.cli.migrate_db

# Apply all pending migrations
python -m app.cli.migrate_db --apply

# Show migration status
python -m app.cli.migrate_db --status

# Fail unless the live database is already at Alembic head
python -m app.cli.migrate_db --check
```

### Options

| Option | Type | Default | Description |
| -------- | ------ | --------- | ------------- |
| `--apply` | flag | false | Apply pending Alembic migrations |
| `--check` | flag | false | Exit non-zero if the database is not at Alembic head |
| `--status` | flag | false | Show current Alembic revision and history |
| `dsn` | string | env | Optional PostgreSQL SQLAlchemy DSN |

### Examples

**Check Status:**

```bash
python -m app.cli.migrate_db --status

# Output:
# Current revision:
# 0001 (head)
```

**Apply All Pending:**

```bash
python -m app.cli.migrate_db --apply

# Output:
# Running database migrations...
# Database migrations complete.
```

### Migration Files

**Location:** `app/db/alembic/versions/`

Alembic owns schema DDL. The old hand-written migration package has been removed.

---

## Search

**Command:** `python -m app.cli.search`

**Purpose:** Test search functionality (Postgres TSVECTOR full-text, vector, hybrid).

### Basic Usage

```bash
# Full-text search
python -m app.cli.search "machine learning"

# Vector search
python -m app.cli.search "neural networks" --mode vector

# Hybrid search
python -m app.cli.search "AI ethics" --mode hybrid
```

### Options

| Option | Type | Default | Description |
| -------- | ------ | --------- | ------------- |
| `query` | string | - | Search query (positional argument) |
| `--mode` | string | fts | Search mode (fts, vector, hybrid) |
| `--limit` | int | 10 | Max results to return |
| `--min-score` | float | 0.0 | Minimum relevance score (0.0-1.0) |
| `--rerank` | flag | false | Apply reranking to results |
| `--lang` | string | auto | Filter by language (en, ru, auto) |
| `--output-format` | string | table | Output format (table, json, compact) |

### Examples

**Full-Text Search:**

```bash
python -m app.cli.search "python tutorial" --limit 5

# Output (table format):
# Rank | Score | Title | URL
# -----| ------- | ---------------------- |-----
#  1   | 0.95 | Python Tutorial 2026 | https://...
#  2   | 0.87 | Learn Python Fast | https://...
#  ...
```

**Vector Search with Reranking:**

```bash
python -m app.cli.search "deep learning frameworks" \
  --mode vector \
  --rerank \
  --limit 10
```

**Hybrid Search (JSON Output):**

```bash
python -m app.cli.search "AI alignment" \
  --mode hybrid \
  --output-format json | jq '.results[].title'
```

**Filter by Language:**

```bash
python -m app.cli.search "машинное обучение" --lang ru
```

### Search Modes

**FTS (Full-Text Search):**

- Postgres TSVECTOR + GIN index on `topic_search_index.body_tsv`
- Fastest (1-5 ms in-cluster)
- Best for exact keyword matches

**Vector:**

- Qdrant vector search on `summary_embeddings`
- Slower (50-200ms)
- Best for semantic similarity

**Hybrid:**

- Combines FTS + Vector results
- Slowest (100-300ms)
- Best for comprehensive search

---

## Backfill Embeddings

**Command:** `python -m app.cli.backfill_embeddings`

**Purpose:** Generate embeddings for existing summaries.

### Basic Usage

```bash
# Backfill all summaries missing embeddings
python -m app.cli.backfill_embeddings

# Rebuild all embeddings (even if existing)
python -m app.cli.backfill_embeddings --rebuild
```

### Options

| Option | Type | Default | Description |
| -------- | ------ | --------- | ------------- |
| `--rebuild` | flag | false | Regenerate all embeddings (skip existing check) |
| `--batch-size` | int | 50 | Embeddings per batch |
| `--model` | string | (from env) | Override embedding model |
| `--limit` | int | - | Limit to N summaries (for testing) |

### Examples

**Backfill Missing Embeddings:**

```bash
python -m app.cli.backfill_embeddings

# Output:
# Found 1234 summaries
# 456 already have embeddings (skipping)
# Generating embeddings for 778 summaries...
# Batch 1/16: 50 embeddings (3.2s)
# Batch 2/16: 50 embeddings (3.1s)
# ...
# Done! Generated 778 embeddings in 2m34s
```

**Rebuild All:**

```bash
python -m app.cli.backfill_embeddings --rebuild

# Output:
# Rebuilding ALL embeddings (1234 summaries)
# This will take approximately 10 minutes.
# Continue? [y/N] y
# ...
```

**Test on Small Batch:**

```bash
python -m app.cli.backfill_embeddings --limit 10

# Output:
# Generating embeddings for 10 summaries (test mode)
# Done! Generated 10 embeddings in 4.2s
```

### Performance

**Typical Speed:**

- CPU (all-MiniLM-L6-v2): ~50 embeddings/sec
- GPU (all-MiniLM-L6-v2): ~200 embeddings/sec

**Memory Usage:**

- all-MiniLM-L6-v2: ~100 MB model + ~10 MB per batch
- all-mpnet-base-v2: ~400 MB model + ~20 MB per batch

---

## Backfill Vector Store

**Command:** `python -m app.cli.backfill_vector_store`

**Purpose:** Populate or refresh Qdrant vector points for searchable content. Scans summary embeddings from Postgres and writes summary points to Qdrant. For backfilling repository vectors, use `backfill_repository_embeddings` followed by this command. See `docs/vector-index-sync.md` for the two-writer architecture (fast path + Taskiq reconciler).

### Basic Usage

```bash
# Backfill all embeddings to Qdrant
python -m app.cli.backfill_vector_store

# Force re-upsert even when Postgres embedding rows already exist
python -m app.cli.backfill_vector_store --force
```

### Options

| Option | Type | Default | Description |
| -------- | ------ | --------- | ------------- |
| `--dsn` | string | `DATABASE_URL` | Override Postgres DSN |
| `--batch-size` | int | 50 | Vectors per Qdrant upsert batch |
| `--qdrant-url` | string | config/env | Override Qdrant URL |
| `--qdrant-api-key` | string | config/env | Override Qdrant API key (prefer env var in automation) |
| `--qdrant-env` | string | config/env | Override environment namespace |
| `--qdrant-scope` | string | config/env | Override user/tenant scope |
| `--qdrant-version` | string | config/env | Override collection version suffix |
| `--limit` | int | - | Limit to N summaries (for testing) |
| `--force` | flag | false | Recompute/re-upsert even when embeddings already exist |
| `--dry-run` | flag | false | Simulate without writing to Qdrant |

### Examples

**Initial Backfill:**

```bash
python -m app.cli.backfill_vector_store

# Output:
# Connecting to Qdrant at http://localhost:6333...
# Collection 'summaries' has 0 points
# Found 1234 summaries with embeddings
# Upserting batch 1/25: 50 vectors (1.2s)
# Upserting batch 2/25: 50 vectors (1.1s)
# ...
# Done! Upserted 1234 documents in 18.4s
```

**Test Connection:**

```bash
python -m app.cli.backfill_vector_store --limit=1

# Output:
# Connecting to Qdrant at http://localhost:6333...
# Connection successful!
# Collection 'summaries' exists
# Upserting 1 document (test mode)
# Success!
```

**Security note:** Some credential-bearing options (such as `--qdrant-api-key`) are intentionally minimized in inline CLI help text. They remain supported, but prefer environment variables/secrets managers in CI and production shells.

**Rebuild note:** this CLI no longer deletes collections. To rebuild from scratch, delete or recreate the Qdrant collection with Qdrant tooling, then run `python -m app.cli.backfill_vector_store --force`.

### Prerequisites

**Qdrant Server:**

```bash
# Start Qdrant server first
docker run -d -p 6333:6333 -p 6334:6334 qdrant/qdrant:v1.12.4

# Or via docker compose
docker compose -f ops/docker/docker-compose.yml up -d qdrant
```

**Environment Variables:**

```bash
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION_VERSION=v1
QDRANT_REQUIRED=true
```

---

## Add Performance Indexes

**Command:** `python -m app.cli.init_userbot_session`

**Purpose:** Initialize a Telethon userbot session for the channel digest subsystem.

### Basic Usage

```bash
# Interactive session initialization (prompts for phone, OTP, 2FA)
python -m app.cli.init_userbot_session
```

### Notes

- This CLI tool requires interactive terminal input (phone number, OTP code, optional 2FA password)
- The preferred alternative is the `/init_session` bot command, which uses a Telegram Mini App to securely relay OTP/2FA codes without exposing them in chat
- The session file is stored at the path configured by `DIGEST_SESSION_NAME` (default: `digest_userbot`)
- Sessions created before the Telethon migration must be recreated. Existing legacy `.session` files are preserved as `<DIGEST_SESSION_NAME>.legacy.bak.session` after a new Telethon session authenticates successfully.

## Check Userbot Session

**Command:** `python -m app.cli.check_userbot_session`

**Purpose:** Verify that the Telethon digest userbot session exists and can connect.

### Basic Usage

```bash
python -m app.cli.check_userbot_session
```

### Exit Codes

- `0`: session exists and `get_me()` succeeds
- `1`: connection/auth check failed
- `2`: session file is missing
- Only needs to be run once; the session persists across restarts

---

## MCP Server

**Command:** `python -m app.cli.mcp_server`

**Purpose:** Start Model Context Protocol (MCP) server for AI agent access.

### Basic Usage

```bash
# Start MCP server (stdio mode, scoped to one user)
MCP_USER_ID=123456789 python -m app.cli.mcp_server

# Start with SSE transport (loopback + user scoped)
python -m app.cli.mcp_server --transport sse --user-id 123456789
```

### Options

| Option | Type | Default | Description |
| -------- | ------ | --------- | ------------- |
| `--transport` | string | stdio | Transport mode (stdio, sse) |
| `--port` | int | 8200 | Port for SSE transport |
| `--host` | string | 127.0.0.1 | Host for SSE transport |
| `--user-id` | int | _(none)_ | Scope all MCP reads to one user |
| `--allow-remote-sse` | flag | false | Allow non-loopback SSE bind host |
| `--allow-unscoped-sse` | flag | false | Allow SSE without explicit user scope |
| `--allow-unscoped-stdio` | flag | false | Allow stdio without explicit user scope |

### Examples

**stdio Mode (Claude Desktop):**

```bash
MCP_USER_ID=123456789 python -m app.cli.mcp_server

# Add to Claude Desktop config:
# ~/.config/claude/claude_desktop_config.json
{
  "mcpServers": {
    "ratatoskr": {
      "command": "python",
      "args": ["-m", "app.cli.mcp_server"],
      "cwd": "/path/to/ratatoskr",
      "env": {
        "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/ratatoskr",
        "MCP_USER_ID": "123456789"
      }
    }
  }
}
```

**SSE Mode (Web Access):**

```bash
python -m app.cli.mcp_server --transport sse --user-id 123456789

# Server starts at http://127.0.0.1:8200
# MCP tools available via HTTP SSE
```

### Available Tools

**Search Tools:**

- `search_articles` - Full-text search by query
- `semantic_search` - Meaning-based vector search
- `find_by_entity` - Search by person/org/location

**Article/Content Tools:**

- `get_article` - Get summary details by ID
- `list_articles` - Paginated list with filters
- `get_article_content` - Full crawled content for summary
- `check_url` - Deduplication lookup by URL

**Organization/Media Tools:**

- `list_collections` - Top-level collections
- `get_collection` - Collection details with items
- `list_videos` - Downloaded video metadata
- `get_video_transcript` - Video transcript text

**Stats Tools:**

- `get_stats` - Database statistics

See: [MCP Server Documentation](mcp-server.md)

---

## Repository Ingest

**Command:** `python -m app.cli.repository`

**Purpose:** Manually ingest a single GitHub repository and run LLM analysis without going through the Telegram bot or API.

**Prerequisites:** Active GitHub integration for the target user (`GITHUB_TOKEN_ENCRYPTION_KEY` set, user has connected via PAT or Device Flow).

### Basic Usage

```bash
python -m app.cli.repository --url https://github.com/owner/repo --user-id 123456789
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--url` | string | required | GitHub repository URL |
| `--user-id` | int | required | Telegram user ID that owns the integration |
| `--json-path` | string | - | Save result JSON to file |
| `--force-reanalyze` | flag | false | Bypass content_hash cache and re-run LLM analysis |

### Exit Codes

- `0` - Success
- `1` - Invalid URL or missing env vars
- `2` - GitHub API error (bad token, repo not found)
- `3` - LLM analysis failed
- `4` - Database error

---

## GitHub Stars Sync

**Command:** `python -m app.cli.sync_github_stars`

**Purpose:** Manually trigger the daily GitHub stars sync that normally runs on the `0 2 * * *` UTC cron schedule (`app/tasks/github_sync.py`). Fetches the starred-repository list from GitHub and enqueues LLM analysis for any new repos.

**Prerequisites:** At least one user must have an active GitHub integration (`GITHUB_TOKEN_ENCRYPTION_KEY` set).

### Basic Usage

```bash
# Sync all users with active integrations
python -m app.cli.sync_github_stars

# Sync one specific user
python -m app.cli.sync_github_stars --user-id 123456789

# Preview what would be synced without writing
python -m app.cli.sync_github_stars --dry-run
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--user-id` | int | - | Limit sync to one user (default: all active integrations) |
| `--dry-run` | flag | false | Print repos that would be ingested without writing to DB |

### Exit Codes

- `0` - Success (or no-op on dry-run)
- `1` - No active integrations found
- `2` - GitHub API error

---

## Backfill Repository Embeddings

**Command:** `python -m app.cli.backfill_repository_embeddings`

**Purpose:** Generate or refresh Postgres-side vector embeddings for repository records that are missing them or have a stale `model_version` (stored in `repository_embeddings`). The analyzer fast path writes Qdrant immediately for new analysis results; run this command after changing the embedding model or after bulk imports, then run `backfill_vector_store` to export analyzed repositories to Qdrant.

**Prerequisites:** `GITHUB_TOKEN_ENCRYPTION_KEY` set; embedding provider configured (`EMBEDDING_PROVIDER`).

### Basic Usage

```bash
# Fill all missing embeddings
python -m app.cli.backfill_repository_embeddings

# Dry-run to count what would be processed
python -m app.cli.backfill_repository_embeddings --dry-run

# Upgrade stale embeddings to a new model version
python -m app.cli.backfill_repository_embeddings --model-version-target v2

# Scope to one user
python -m app.cli.backfill_repository_embeddings --user-id 123456789 --batch-size 25
```

### Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--dry-run` | flag | false | Print counts without writing |
| `--batch-size` | int | 50 | Repositories per embedding batch |
| `--model-version-target` | string | current | Re-embed rows whose `model_version` differs |
| `--user-id` | int | - | Limit to one user |

### Exit Codes

- `0` - Success
- `1` - Missing env vars
- `2` - Embedding service error

---

## Common Patterns

### Debugging Failed Summarization

```bash
# 1. Try CLI runner with debug logging
python -m app.cli.summary \
  --url <URL> \
  --log-level DEBUG

# 2. Check database for errors
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT * FROM requests WHERE input_url = '<URL>';"

# 3. Check LLM calls
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT error_text FROM llm_calls
     WHERE request_id = (SELECT id FROM requests WHERE correlation_id = '<correlation_id>')
     ORDER BY attempt_index;"
```

### Testing Search Performance

```bash
# 1. Backfill embeddings if missing
python -m app.cli.backfill_embeddings

# 2. Backfill Qdrant
python -m app.cli.backfill_vector_store

# 3. Benchmark
time python -m app.cli.search "test query" --mode vector
```

### Database Maintenance

```bash
# Apply PostgreSQL migrations
python -m app.cli.migrate_db
```

---

## Environment Variables

**All CLI tools respect these environment variables:**

```bash
# Database
DB_PATH=/data/ratatoskr.db

# Logging
LOG_LEVEL=INFO         # DEBUG for CLI debugging

# Qdrant
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION_VERSION=v1
QDRANT_REQUIRED=true

# LLM (for summary CLI)
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=deepseek/deepseek-v4-flash

# Scraper sidecars (optional; in-process Scrapling works without them)
FIRECRAWL_SELF_HOSTED_ENABLED=true
FIRECRAWL_SELF_HOSTED_URL=http://firecrawl-api:3002
SCRAPER_CRAWL4AI_URL=http://crawl4ai:11235
SCRAPER_DEFUDDLE_API_BASE_URL=http://defuddle-api:3003
```

---

## Exit Codes

**Standard Exit Codes:**

- `0` - Success
- `1` - Validation error (invalid arguments, missing env vars)
- `2` - External service error (Firecrawl, OpenRouter, Qdrant)
- `3` - Database error (connection failed, query failed)
- `4` - Internal error (unexpected exception)

**Example:**

```bash
python -m app.cli.summary --url invalid-url
echo $?  # 1 (validation error)

python -m app.cli.summary --url https://example.com
echo $?  # 0 (success)
```

---

## See Also

- [How-To Guides](../guides/) - Step-by-step task guides
- [TROUBLESHOOTING](troubleshooting.md) - Debugging guide
- [Environment Variables](environment-variables.md) - Configuration reference

---

**Last Updated:** 2026-02-09
