# Ratatoskr — Personal Knowledge Archive

## Description

Ratatoskr stores structured summaries and extracted content for web pages,
videos, aggregation bundles, X bookmarks, and scored source signals. This skill
connects OpenClaw to Ratatoskr's MCP server for scoped archive search, retrieval,
research, ingestion, feedback, and vector-index diagnostics.

The current MCP surface exposes 28 tools and 17 resources. Every request is
scoped to a Ratatoskr user. Expensive search and ingestion tools have tighter
in-process rate limits than ordinary reads.

## Capabilities

- Search the archive with PostgreSQL full-text, Qdrant semantic, or hybrid search.
- Retrieve article summaries, extracted content, collections, and video transcripts.
- Find related articles and research questions against the archive with citations.
- Create and inspect multi-source aggregation bundles.
- Search ingested X bookmarks and promote bookmarks or signals into the library.
- Review signal sources and candidates, record feedback, and enable or disable sources.
- Inspect vector health, index coverage, and PostgreSQL/Qdrant synchronization gaps.
- Check normalized URLs before submitting duplicate work.

## Available Tools

| Tool | Parameters | Purpose |
| --- | --- | --- |
| `create_aggregation_bundle` | `items`, `lang_preference="auto"`, `metadata=null` | Create and run a multi-source aggregation bundle. |
| `get_aggregation_bundle` | `session_id` | Get one persisted aggregation bundle. |
| `list_aggregation_bundles` | `limit=20`, `offset=0`, `status=null` | List the scoped user's aggregation bundles. |
| `check_source_supported` | `url`, `source_kind_hint=null` | Classify a URL against the public aggregation source contract. |
| `search_articles` | `query`, `limit=10` | Search summaries by keyword, topic, or entity using PostgreSQL full-text search. |
| `get_article` | `summary_id` | Get the structured summary for an article. |
| `list_articles` | `limit=20`, `offset=0`, `is_favorited=null`, `lang=null`, `tag=null` | List summaries with optional filters. |
| `get_article_content` | `summary_id` | Get the extracted markdown/text behind a summary. |
| `get_stats` | none | Get archive statistics. |
| `find_by_entity` | `entity_name`, `entity_type=null`, `limit=10` | Find summaries mentioning a person, organization, or location. |
| `x_search` | `query`, `category=null`, `limit=10` | Search ingested X bookmarks with PostgreSQL full-text search. |
| `ask_my_archive` | `query`, `max_sources=12` | Research the scoped archive and return an answer with verified citations. |
| `list_collections` | `limit=20`, `offset=0` | List article collections. |
| `get_collection` | `collection_id`, `include_items=true`, `limit=50` | Get a collection and optionally its summaries. |
| `list_videos` | `limit=20`, `offset=0`, `status=null` | List downloaded YouTube videos. |
| `get_video_transcript` | `video_id` | Get a cached video transcript. |
| `check_url` | `url` | Normalize a URL and check whether it was already processed. |
| `semantic_search` | `description`, `limit=10`, `language=null`, `min_similarity=0.25`, `rerank=false`, `include_chunks=true` | Search by meaning with Qdrant and the configured fallback strategy. |
| `hybrid_search` | `query`, `limit=10`, `language=null`, `min_similarity=0.25`, `rerank=false` | Merge keyword and semantic retrieval into one ranking. |
| `find_similar_articles` | `summary_id`, `limit=10`, `min_similarity=0.3`, `rerank=false`, `include_chunks=true` | Find summaries semantically similar to an existing article. |
| `list_signal_sources` | `limit=50` | List the scoped user's signal sources. |
| `list_user_signals` | `limit=20`, `status=null` | List scored signal candidates. |
| `update_signal_feedback` | `signal_id`, `action` | Record `like`, `dislike`, `skip`, `queue`, or `hide_source`. |
| `promote_to_library` | `source_type`, `source_id` | Queue a signal or X bookmark for durable summarization. |
| `set_signal_source_active` | `source_id`, `is_active` | Enable or disable a subscribed signal source. |
| `vector_health` | none | Report vector-store availability and fallback readiness. |
| `vector_index_stats` | `scan_limit=5000` | Compare indexed vectors with PostgreSQL summaries. |
| `vector_sync_gap` | `max_scan=5000`, `sample_size=20` | Sample missing or stale vector-index entries. |

## Available Resources

| URI | Description |
| --- | --- |
| `ratatoskr://aggregations/recent` | Recent aggregation bundles for the scoped user. |
| `ratatoskr://aggregations/{session_id}` | One persisted aggregation bundle. |
| `ratatoskr://articles/recent` | Ten most recent article summaries. |
| `ratatoskr://articles/favorites` | Favorited summaries, up to 50. |
| `ratatoskr://articles/unread` | Unread summaries, up to 20. |
| `ratatoskr://stats` | Current archive statistics. |
| `ratatoskr://tags` | Topic tags with article counts. |
| `ratatoskr://entities` | Aggregated people, organizations, and locations. |
| `ratatoskr://domains` | Source domains with article counts. |
| `ratatoskr://collections` | Top-level collections with item counts. |
| `ratatoskr://videos/recent` | Ten most recent completed video downloads. |
| `ratatoskr://processing/stats` | LLM calls, token use, costs, and model breakdown. |
| `ratatoskr://vector/health` | Vector-store availability. |
| `ratatoskr://vector/index-stats` | Vector coverage compared with PostgreSQL. |
| `ratatoskr://vector/sync-gap` | Default vector/PostgreSQL gap sample. |
| `ratatoskr://signals/recent` | Recent signal candidates for the scoped user. |
| `ratatoskr://sources` | Signal source catalog. |

## Summary Data

Article payloads use the current summary contract from
`app/core/summary_schema.py`. Core fields include `summary_250`,
`summary_1000`, `tldr`, `key_ideas`, `topic_tags`, `entities`,
`estimated_reading_time_min`, `key_stats`, `answered_questions`, `readability`,
and `seo_keywords`; the complete contract also contains confidence, grounding,
quality, and enrichment fields.

## Setup

### stdio transport (recommended for local OpenClaw)

Use the repository virtual environment, a PostgreSQL async DSN, and an explicit
Ratatoskr user scope. Replace both placeholders:

```json
{
  "mcpServers": {
    "ratatoskr": {
      "command": ".venv/bin/python",
      "args": [
        "-m",
        "app.cli.mcp_server",
        "--user-id",
        "<telegram-user-id>"
      ],
      "cwd": "/path/to/ratatoskr",
      "env": {
        "DATABASE_URL": "postgresql+asyncpg://user:password@localhost:5432/ratatoskr"
      }
    }
  }
}
```

`--user-id` may be replaced by `MCP_USER_ID` in the environment. Unscoped stdio
is rejected unless `--allow-unscoped-stdio` is explicitly supplied; keep normal
OpenClaw integrations scoped. The removed SQLite `DB_PATH` option is not
supported. `--dsn` is the CLI alternative to `DATABASE_URL`.

### SSE transport (trusted loopback)

```bash
DATABASE_URL='postgresql+asyncpg://user:password@localhost:5432/ratatoskr' \
  .venv/bin/python -m app.cli.mcp_server \
  --transport sse \
  --host 127.0.0.1 \
  --port 8200 \
  --user-id <telegram-user-id>
```

Configure the client for `http://127.0.0.1:8200/sse`. For non-loopback hosted
SSE, use `--auth-mode jwt --allow-remote-sse` and the documented JWT/forwarding
headers; do not expose a fixed-user or unscoped server publicly.

### Semantic and hybrid search

Semantic tools use Qdrant when configured:

```bash
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=your-qdrant-token  # optional for an unsecured local Qdrant
QDRANT_ENV=production
QDRANT_USER_SCOPE=public
```

Use `vector_health`, `vector_index_stats`, and `vector_sync_gap` to distinguish
an unavailable store from an incomplete index. Search services retain their
configured keyword/local fallback behavior when the vector backend is not ready.
