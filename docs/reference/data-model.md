# Data Model Reference

Complete reference for Ratatoskr's PostgreSQL database schema.

**Audience:** Developers, Database Administrators **Type:** Reference **Related:** [SPEC.md § Data Model](../SPEC.md#data-model), [How to Backup and Restore](../guides/backup-and-restore.md)

---

## Overview

Ratatoskr uses **PostgreSQL 16** as its relational persistence layer with SQLAlchemy 2.0 models. This page documents the core tables that drive the URL pipeline, mobile API, signal scoring, and audit surface; the full registered set lives in `ALL_MODELS` in `app/db/models/__init__.py` and includes additional channel-digest, RSS, webhook, automation, and user-preference tables not detailed here.

**Database DSN:** `DATABASE_URL` or `POSTGRES_PASSWORD`-derived Compose DSN

**ORM:** SQLAlchemy 2.0 async ORM **Migrations:** Alembic revisions in `app/db/alembic/versions/`

---

## Core Tables

### users

**Purpose:** Telegram users who have interacted with the bot.

**Schema:**

```sql
CREATE TABLE users (
    telegram_user_id  INTEGER PRIMARY KEY,
    username          TEXT,
    first_name        TEXT,
    last_name         TEXT,
    language_code     TEXT,
    is_owner          INTEGER DEFAULT 0,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `telegram_user_id` (int, PK) - Telegram user ID
- `username` (str, nullable) - Telegram @username
- `first_name` (str, nullable) - User's first name
- `last_name` (str, nullable) - User's last name
- `language_code` (str, nullable) - Telegram language code (e.g., `en`, `ru`)
- `is_owner` (bool) - Owner/admin rollout flag used by privileged bot and auth-management paths. External JWT API and hosted MCP auth can still run multi-user when `ALLOWED_USER_IDS` is empty.
- `created_at` (datetime) - First interaction timestamp
- `updated_at` (datetime) - Last update timestamp

**Indexes:**

- Primary key on `telegram_user_id`

**Relationships:**

- One-to-many with `requests`
- One-to-many with `user_interactions`
- One-to-many with `user_devices`

---

### chats

**Purpose:** Telegram chats (private DMs, groups, channels) where bot is active.

**Schema:**

```sql
CREATE TABLE chats (
    chat_id     INTEGER PRIMARY KEY,
    type        TEXT NOT NULL,  -- 'private', 'group', 'supergroup', 'channel'
    title       TEXT,
    username    TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `chat_id` (int, PK) - Telegram chat ID
- `type` (str) - Chat type (`private`, `group`, `supergroup`, `channel`)
- `title` (str, nullable) - Chat title (for groups/channels)
- `username` (str, nullable) - Chat @username
- `created_at` (datetime) - First interaction timestamp

**Indexes:**

- Primary key on `chat_id`

**Relationships:**

- One-to-many with `requests`
- One-to-many with `telegram_messages`

---

### requests

**Purpose:** One row per user submission (URL or forwarded message).

**Schema:**

```sql
CREATE TABLE requests (
    id                         TEXT PRIMARY KEY,
    created_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    type                       TEXT NOT NULL,  -- 'url' | 'forward'
    status                     TEXT DEFAULT 'pending',  -- 'pending'| 'ok' |'error'
    chat_id                    INTEGER REFERENCES chats(chat_id),
    user_id                    INTEGER REFERENCES users(telegram_user_id),
    input_url                  TEXT,
    normalized_url             TEXT,
    dedupe_hash                TEXT,  -- sha256(normalized_url)
    input_message_id           INTEGER,
    fwd_from_chat_id           INTEGER,
    fwd_from_msg_id            INTEGER,
    lang_detected              TEXT,  -- 'en', 'ru', etc.
    route_version              INTEGER DEFAULT 1,
    total_processing_time_sec  REAL,
    error_message              TEXT
);
```

**Fields:**

- `id` (str, PK) - Unique request ID (correlation ID)
- `created_at` (datetime) - Request creation timestamp
- `type` (str) - Request type (`url` or `forward`)
- `status` (str) - Processing status (`pending`, `ok`, `error`)
- `chat_id` (int, FK) - Foreign key to `chats`
- `user_id` (int, FK) - Foreign key to `users`
- `input_url` (str, nullable) - Original URL as submitted
- `normalized_url` (str, nullable) - Normalized URL (lowercased, params sorted)
- `dedupe_hash` (str, nullable) - SHA256 hash of `normalized_url` for deduplication
- `input_message_id` (int, nullable) - Telegram message ID
- `fwd_from_chat_id` (int, nullable) - Forwarded from chat ID
- `fwd_from_msg_id` (int, nullable) - Forwarded message ID
- `lang_detected` (str, nullable) - Detected language code
- `route_version` (int) - Message router version
- `total_processing_time_sec` (float, nullable) - End-to-end processing time
- `error_message` (str, nullable) - Error details if `status='error'`

**Indexes:**

```sql
CREATE INDEX idx_requests_user_id ON requests(user_id);
CREATE INDEX idx_requests_created_at ON requests(created_at);
CREATE INDEX idx_requests_dedupe_hash ON requests(dedupe_hash);
CREATE INDEX idx_requests_status ON requests(status);
```

**Relationships:**

- Many-to-one with `users`
- Many-to-one with `chats`
- One-to-many with `aggregation_session_items`
- One-to-one with `telegram_messages`
- One-to-one with `crawl_results`
- One-to-one with `video_downloads`
- One-to-many with `llm_calls`
- One-to-one with `summaries`

---

### aggregation_sessions

**Purpose:** Stores bundle-level state for mixed-source aggregation runs.

**Schema:**

```sql
CREATE TABLE aggregation_sessions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    correlation_id        TEXT NOT NULL UNIQUE,
    total_items           INTEGER NOT NULL,
    successful_count      INTEGER NOT NULL DEFAULT 0,
    failed_count          INTEGER NOT NULL DEFAULT 0,
    duplicate_count       INTEGER NOT NULL DEFAULT 0,
    allow_partial_success INTEGER NOT NULL DEFAULT 1,
    status                TEXT NOT NULL DEFAULT 'pending',
    bundle_metadata_json  JSON,
    aggregation_output_json JSON,
    failure_code          TEXT,
    failure_message       TEXT,
    failure_details_json  JSON,
    queued_at             TIMESTAMP,
    started_at            TIMESTAMP,
    completed_at          TIMESTAMP,
    last_progress_at      TIMESTAMP,
    progress_percent      INTEGER,
    processing_time_ms    INTEGER,
    updated_at            TIMESTAMP NOT NULL,
    created_at            TIMESTAMP NOT NULL
);
```

**Fields:**

- `correlation_id` (str, unique) - Stable bundle correlation ID
- `total_items` (int) - Number of submitted source items, including duplicates
- `successful_count` / `failed_count` / `duplicate_count` (int) - Bundle rollup counters
- `allow_partial_success` (bool) - Whether one failed item may coexist with successful items
- `status` (str) - Bundle lifecycle (`pending`, `processing`, `completed`, `partial`, `failed`, `cancelled`)
- `bundle_metadata_json` (json, nullable) - Submission metadata for the bundle
- `failure_*` (nullable) - Bundle-level failure details surfaced to callers and logs
- `queued_at` / `started_at` / `completed_at` / `last_progress_at` (datetime, nullable) - Lifecycle timestamps used by the external API, CLI, and MCP surfaces
- `progress_percent` (int, nullable) - Persisted bundle-level completion percentage used for external progress reporting
- `processing_time_ms` (int, nullable) - End-to-end latency once extraction plus synthesis reaches a terminal state

**Indexes:**

```sql
CREATE INDEX idx_aggregation_sessions_user ON aggregation_sessions(user_id);
CREATE INDEX idx_aggregation_sessions_status ON aggregation_sessions(status);
CREATE INDEX idx_aggregation_sessions_created ON aggregation_sessions(created_at);
```

---

### aggregation_session_items

**Purpose:** Stores ordered source items, dedupe state, normalized extraction payloads, and item-level failures inside one aggregation bundle.

**Schema:**

```sql
CREATE TABLE aggregation_session_items (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    aggregation_session_id  INTEGER NOT NULL REFERENCES aggregation_sessions(id) ON DELETE CASCADE,
    request_id              INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    position                INTEGER NOT NULL,
    source_kind             TEXT NOT NULL,
    source_item_id          TEXT NOT NULL,
    source_dedupe_key       TEXT NOT NULL,
    original_value          TEXT,
    normalized_value        TEXT,
    external_id             TEXT,
    telegram_chat_id        INTEGER,
    telegram_message_id     INTEGER,
    telegram_media_group_id TEXT,
    title_hint              TEXT,
    source_metadata_json    JSON,
    normalized_document_json JSON,
    extraction_metadata_json JSON,
    status                  TEXT NOT NULL DEFAULT 'pending',
    duplicate_of_item_id    INTEGER,
    failure_code            TEXT,
    failure_message         TEXT,
    failure_details_json    JSON,
    updated_at              TIMESTAMP NOT NULL,
    created_at              TIMESTAMP NOT NULL
);
```

**Fields:**

- `source_kind` (str) - Shared source taxonomy (`x_post`, `x_article`, `threads_post`, `instagram_*`, `web_article`, `telegram_*`, `youtube_video`)
- `source_item_id` (str) - Stable hashed source identity
- `source_dedupe_key` (str) - Natural dedupe key used within the session
- `request_id` (int, nullable) - Linked extraction request row when one exists
- `status` (str) - Item lifecycle (`pending`, `processing`, `extracted`, `failed`, `duplicate`, `skipped`)
- `duplicate_of_item_id` (int, nullable) - First matching source item position inside the same session
- `normalized_document_json` (json, nullable) - Stored `NormalizedSourceDocument` payload from the extractor contract
- `failure_*` (nullable) - Item-level failure diagnostics

**Indexes:**

```sql
CREATE UNIQUE INDEX idx_aggregation_session_items_position
  ON aggregation_session_items(aggregation_session_id, position);
CREATE INDEX idx_aggregation_session_items_source_item
  ON aggregation_session_items(aggregation_session_id, source_item_id);
CREATE INDEX idx_aggregation_session_items_request ON aggregation_session_items(request_id);
CREATE INDEX idx_aggregation_session_items_status ON aggregation_session_items(status);
```

**Dedupe Rules:**

- URL-backed sources dedupe on normalized URL unless a stronger platform identifier exists (`external_id` such as tweet ID or YouTube video ID).
- Telegram-native sources dedupe on `(telegram_chat_id, telegram_message_id)` or `(telegram_chat_id, telegram_media_group_id)`.
- Duplicates are preserved as rows with `status='duplicate'` so callers can surface that input was received but merged.

---

### sources

**Purpose:** Generic Phase 3 source table for proactive signal scoring. It coexists with the legacy `rss_feeds` and `channels` tables while callers are migrated.

**Schema summary:**

```sql
CREATE TABLE sources (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                  TEXT NOT NULL, -- rss | telegram_channel
    external_id           TEXT,
    url                   TEXT,
    title                 TEXT,
    description           TEXT,
    site_url              TEXT,
    is_active             INTEGER NOT NULL DEFAULT 1,
    fetch_error_count     INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT,
    last_fetched_at       TIMESTAMP,
    last_successful_at    TIMESTAMP,
    metadata_json         JSON,
    legacy_rss_feed_id    INTEGER UNIQUE REFERENCES rss_feeds(id) ON DELETE SET NULL,
    legacy_channel_id     INTEGER UNIQUE REFERENCES channels(id) ON DELETE SET NULL,
    updated_at            TIMESTAMP NOT NULL,
    created_at            TIMESTAMP NOT NULL
);
```

**Indexes and constraints:**

- Unique `(kind, external_id)` for natural source identity.
- Non-unique `(kind, is_active)` for ingestion scans.
- `legacy_rss_feed_id` and `legacy_channel_id` cross-reference the source row to the original `rss_feeds` / `channels` table entry, populated by `feed_poller` and `signal_ingester` and propagated as ingestion metadata.

---

### subscriptions

**Purpose:** Single-user subscription link from `users` to generic `sources`.

**Schema summary:**

```sql
CREATE TABLE subscriptions (
    id                              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                         INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    source_id                       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    is_active                       INTEGER NOT NULL DEFAULT 1,
    cadence_seconds                 INTEGER,
    next_fetch_at                   TIMESTAMP,
    topic_constraints_json          JSON,
    metadata_json                   JSON,
    legacy_rss_subscription_id      INTEGER UNIQUE REFERENCES rss_feed_subscriptions(id) ON DELETE SET NULL,
    legacy_channel_subscription     INTEGER UNIQUE,
    updated_at                      TIMESTAMP NOT NULL,
    created_at                      TIMESTAMP NOT NULL
);
```

**Indexes and constraints:**

- Unique `(user_id, source_id)`.
- Non-unique `(user_id, is_active)` and `next_fetch_at`.

---

### feed_items

**Purpose:** Generic ingested item table for RSS items and Telegram channel posts before user-specific signal scoring.

**Schema summary:**

```sql
CREATE TABLE feed_items (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id                INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    external_id              TEXT NOT NULL,
    canonical_url            TEXT,
    title                    TEXT,
    content_text             TEXT,
    author                   TEXT,
    published_at             TIMESTAMP,
    views                    INTEGER,
    forwards                 INTEGER,
    comments                 INTEGER,
    engagement_score         REAL,
    metadata_json            JSON,
    legacy_rss_item_id       INTEGER UNIQUE REFERENCES rss_feed_items(id) ON DELETE SET NULL,
    legacy_channel_post_id   INTEGER UNIQUE REFERENCES channel_posts(id) ON DELETE SET NULL,
    updated_at               TIMESTAMP NOT NULL,
    created_at               TIMESTAMP NOT NULL
);
```

**Indexes and constraints:**

- Unique `(source_id, external_id)`.
- Non-unique `published_at` and `canonical_url`.

---

### topics

**Purpose:** Single-user interest topics used by signal scoring and vector-backed personalization.

**Schema summary:**

```sql
CREATE TABLE topics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    weight          REAL NOT NULL DEFAULT 1.0,
    embedding_ref   TEXT,
    metadata_json   JSON,
    is_active       INTEGER NOT NULL DEFAULT 1,
    updated_at      TIMESTAMP NOT NULL,
    created_at      TIMESTAMP NOT NULL
);
```

**Indexes and constraints:**

- Unique `(user_id, name)`.
- Non-unique `(user_id, is_active)`.

---

### user_signals

**Purpose:** Per-user scoring decision for a `feed_items` row. It records deterministic evidence and, once the LLM judge is wired, bounded judge metadata and cost.

**Schema summary:**

```sql
CREATE TABLE user_signals (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    feed_item_id       INTEGER NOT NULL REFERENCES feed_items(id) ON DELETE CASCADE,
    topic_id           INTEGER REFERENCES topics(id) ON DELETE SET NULL,
    status             TEXT NOT NULL DEFAULT 'candidate',
    heuristic_score    REAL,
    llm_score          REAL,
    final_score        REAL,
    filter_stage       TEXT NOT NULL DEFAULT 'heuristic',
    evidence_json      JSON,
    llm_judge_json     JSON,
    llm_cost_usd       REAL,
    decided_at         TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL,
    created_at         TIMESTAMP NOT NULL
);
```

**Indexes and constraints:**

- Unique `(user_id, feed_item_id)`.
- Non-unique `(user_id, status)` and `final_score`.
- Signal scoring fails closed when vector topic similarity is unavailable; it does not silently degrade to SQLite-only matching.

**Migration and rollback notes:**

- The SQLAlchemy Alembic baseline creates these five tables alongside the rest of the PostgreSQL schema.
- Legacy RSS/channel tables are preserved and remain the runtime source for existing API and bot paths until worker/API integration is complete.
- Downgrade behavior is controlled by Alembic revision history; take a normal PostgreSQL backup before applying migrations on a live host.

---

### telegram_messages

**Purpose:** Full Telegram message snapshot for audit trail.

**Schema:**

```sql
CREATE TABLE telegram_messages (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id                TEXT UNIQUE REFERENCES requests(id),
    message_id                INTEGER,
    chat_id                   INTEGER,
    user_id                   INTEGER,
    date_ts                   INTEGER,  -- Unix timestamp
    text_full                 TEXT,
    entities_json             TEXT,  -- JSON array of message entities
    media_type                TEXT,  -- 'photo', 'video', 'document', etc.
    media_file_ids_json       TEXT,  -- JSON array of file IDs
    forward_from_chat_id      INTEGER,
    forward_from_chat_type    TEXT,
    forward_from_chat_title   TEXT,
    forward_from_message_id   INTEGER,
    forward_date_ts           INTEGER,
    message_snapshot          TEXT,  -- Full Telegram message JSON
    created_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (int, PK, autoincrement) - Internal ID
- `request_id` (str, FK, unique) - Foreign key to `requests`
- `message_id` (int) - Telegram message ID
- `chat_id` (int) - Telegram chat ID
- `user_id` (int) - Telegram user ID
- `date_ts` (int) - Message timestamp (Unix epoch)
- `text_full` (str, nullable) - Full message text
- `entities_json` (str, nullable) - JSON array of message entities (mentions, URLs, etc.)
- `media_type` (str, nullable) - Media type if present
- `media_file_ids_json` (str, nullable) - JSON array of file IDs
- `forward_from_chat_id` (int, nullable) - Forwarded from chat ID
- `forward_from_chat_type` (str, nullable) - Forwarded from chat type
- `forward_from_chat_title` (str, nullable) - Forwarded from chat title
- `forward_from_message_id` (int, nullable) - Forwarded message ID
- `forward_date_ts` (int, nullable) - Original forward timestamp
- `message_snapshot` (str) - Full Telegram message object as JSON
- `created_at` (datetime) - Record creation timestamp

**Indexes:**

```sql
CREATE UNIQUE INDEX idx_telegram_messages_request_id ON telegram_messages(request_id);
CREATE INDEX idx_telegram_messages_message_id ON telegram_messages(message_id);
```

**Relationships:**

- One-to-one with `requests`

---

### crawl_results

**Purpose:** Firecrawl API responses for content extraction.

**Schema:**

```sql
CREATE TABLE crawl_results (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id               TEXT UNIQUE REFERENCES requests(id),
    source_url               TEXT NOT NULL,
    endpoint                 TEXT DEFAULT '/v2/scrape',
    http_status              INTEGER,
    status                   TEXT,  -- 'ok'|'error'
    options_json             TEXT,  -- Firecrawl request options
    content_markdown         TEXT,  -- Extracted markdown content
    content_html             TEXT,  -- Extracted HTML content
    structured_json          TEXT,  -- Structured data extraction result
    metadata_json            TEXT,  -- Page metadata (title, description, etc.)
    links_json               TEXT,  -- Extracted links
    screenshots_paths_json   TEXT,  -- Screenshot file paths
    firecrawl_success        INTEGER,  -- 0/1 boolean
    firecrawl_error_code     TEXT,
    firecrawl_error_message  TEXT,
    firecrawl_details_json   TEXT,  -- Error details
    raw_response_json        TEXT,  -- Full Firecrawl response; nulled by migration 006 once structured fields are decomposed
    tokens_used              INTEGER,
    latency_ms               INTEGER,
    error_text               TEXT,  -- Internal error message
    created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (int, PK, autoincrement) - Internal ID
- `request_id` (str, FK, unique) - Foreign key to `requests`
- `source_url` (str) - URL crawled
- `endpoint` (str) - Firecrawl endpoint (`/v2/scrape`)
- `http_status` (int, nullable) - HTTP response status code
- `status` (str) - Internal status (`ok` or `error`)
- `options_json` (str, nullable) - Firecrawl request options as JSON
- `content_markdown` (str, nullable) - Extracted markdown content
- `content_html` (str, nullable) - Extracted HTML content
- `structured_json` (str, nullable) - Structured data extraction
- `metadata_json` (str, nullable) - Page metadata (title, description, og tags, etc.)
- `links_json` (str, nullable) - Extracted links as JSON array
- `screenshots_paths_json` (str, nullable) - Screenshot file paths
- `firecrawl_success` (bool) - Firecrawl success flag
- `firecrawl_error_code` (str, nullable) - Firecrawl error code
- `firecrawl_error_message` (str, nullable) - Firecrawl error message
- `firecrawl_details_json` (str, nullable) - Firecrawl error details
- `raw_response_json` (str, nullable) - Full Firecrawl response; nulled by migration 006 after structured fields are decomposed; readers fall back to it for rows pre-decomposition
- `tokens_used` (int, nullable) - Tokens consumed
- `latency_ms` (int, nullable) - API call latency
- `error_text` (str, nullable) - Internal error message
- `created_at` (datetime) - Record creation timestamp

**Indexes:**

```sql
CREATE UNIQUE INDEX idx_crawl_results_request_id ON crawl_results(request_id);
```

**Relationships:**

- One-to-one with `requests`

---

### video_downloads

**Purpose:** YouTube video downloads and transcript extraction.

**Schema:**

```sql
CREATE TABLE video_downloads (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id                 TEXT UNIQUE REFERENCES requests(id),
    created_at                 TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    video_id                   TEXT NOT NULL,  -- YouTube video ID (11 chars)
    status                     TEXT DEFAULT 'pending',  -- 'pending'| 'downloading' | 'completed' |'error'
    video_file_path            TEXT,
    subtitle_file_path         TEXT,
    metadata_file_path         TEXT,
    thumbnail_file_path        TEXT,
    title                      TEXT,
    channel                    TEXT,
    channel_id                 TEXT,
    duration_sec               INTEGER,
    upload_date                TEXT,  -- YYYYMMDD format
    view_count                 INTEGER,
    like_count                 INTEGER,
    resolution                 TEXT,  -- '1080p', '720p', etc.
    file_size_bytes            INTEGER,
    video_codec                TEXT,  -- 'avc1', 'vp9', etc.
    audio_codec                TEXT,  -- 'mp4a', 'opus', etc.
    format_id                  TEXT,  -- yt-dlp format ID
    transcript_text            TEXT,  -- Full transcript
    transcript_source          TEXT,  -- 'youtube-transcript-api' | 'yt-dlp-subtitles'
    subtitle_language          TEXT,  -- 'en', 'ru', etc.
    auto_generated             INTEGER,  -- 0/1 boolean
    download_started_at        TIMESTAMP,
    download_completed_at      TIMESTAMP,
    error_text                 TEXT
);
```

**Fields:**

- `id` (int, PK, autoincrement) - Internal ID
- `request_id` (str, FK, unique) - Foreign key to `requests`
- `created_at` (datetime) - Record creation timestamp
- `video_id` (str) - YouTube video ID (11 characters)
- `status` (str) - Download status
- `video_file_path` (str, nullable) - Path to downloaded MP4 file
- `subtitle_file_path` (str, nullable) - Path to subtitle/caption VTT file
- `metadata_file_path` (str, nullable) - Path to yt-dlp metadata JSON
- `thumbnail_file_path` (str, nullable) - Path to thumbnail image
- `title` (str, nullable) - Video title
- `channel` (str, nullable) - Channel name
- `channel_id` (str, nullable) - YouTube channel ID
- `duration_sec` (int, nullable) - Video duration in seconds
- `upload_date` (str, nullable) - Upload date (YYYYMMDD)
- `view_count` (int, nullable) - View count at download time
- `like_count` (int, nullable) - Like count at download time
- `resolution` (str, nullable) - Video resolution
- `file_size_bytes` (int, nullable) - Downloaded file size
- `video_codec` (str, nullable) - Video codec
- `audio_codec` (str, nullable) - Audio codec
- `format_id` (str, nullable) - yt-dlp format ID used
- `transcript_text` (str, nullable) - Full transcript text
- `transcript_source` (str, nullable) - Transcript source
- `subtitle_language` (str, nullable) - Subtitle language code
- `auto_generated` (bool, nullable) - Transcript auto-generated flag
- `download_started_at` (datetime, nullable) - Download start time
- `download_completed_at` (datetime, nullable) - Download completion time
- `error_text` (str, nullable) - Error message if failed

**Indexes:**

```sql
CREATE UNIQUE INDEX idx_video_downloads_request_id ON video_downloads(request_id);
CREATE INDEX idx_video_downloads_video_id ON video_downloads(video_id);
```

**Relationships:**

- One-to-one with `requests`

---

### llm_calls

**Purpose:** LLM provider calls for summarization. OpenRouter is the default provider, while OpenAI, Anthropic, and Ollama-compatible endpoints can also populate the same table through `LLMClientProtocol`.

**Schema:**

```sql
CREATE TABLE llm_calls (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id               TEXT REFERENCES requests(id),
    provider                 TEXT DEFAULT 'openrouter',
    model                    TEXT NOT NULL,
    endpoint                 TEXT DEFAULT '/api/v1/chat/completions',
    request_headers_json     TEXT,  -- Authorization redacted
    request_messages_json    TEXT,  -- Chat messages array
    request_full_json        TEXT,  -- Full request payload
    response_text            TEXT,  -- Assistant response text
    response_json            TEXT,  -- Full response payload
    prompt_tokens            INTEGER,
    completion_tokens        INTEGER,
    total_tokens             INTEGER,
    cost_usd                 REAL,
    latency_ms               INTEGER,
    status                   TEXT DEFAULT 'ok',  -- 'ok'|'error'
    error_message            TEXT,
    created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (int, PK, autoincrement) - Internal ID
- `request_id` (str, FK) - Foreign key to `requests`
- `provider` (str) - LLM provider (`openrouter`, `openai`, `anthropic`, or `ollama`)
- `model` (str) - Model name (e.g., `deepseek/deepseek-v4-flash`)
- `endpoint` (str) - API endpoint
- `request_headers_json` (str, nullable) - Request headers (Authorization redacted)
- `request_messages_json` (str) - Chat messages array as JSON
- `request_full_json` (str, nullable) - Full request payload
- `response_text` (str, nullable) - Assistant response text
- `response_json` (str, nullable) - Full response payload
- `prompt_tokens` (int, nullable) - Input tokens used
- `completion_tokens` (int, nullable) - Output tokens used
- `total_tokens` (int, nullable) - Total tokens used
- `cost_usd` (float, nullable) - Estimated cost in USD
- `latency_ms` (int, nullable) - API call latency
- `status` (str) - Call status (`ok` or `error`)
- `error_message` (str, nullable) - Error details if failed
- `created_at` (datetime) - Record creation timestamp

**Indexes:**

```sql
CREATE INDEX idx_llm_calls_request_id ON llm_calls(request_id);
CREATE INDEX idx_llm_calls_created_at ON llm_calls(created_at);
CREATE INDEX idx_llm_calls_model ON llm_calls(model);
```

**Relationships:**

- Many-to-one with `requests`

---

### summaries

**Purpose:** Final validated summary JSON sent to user.

**Schema:**

```sql
CREATE TABLE summaries (
    id               TEXT PRIMARY KEY,
    request_id       TEXT UNIQUE REFERENCES requests(id),
    lang             TEXT NOT NULL,  -- 'en', 'ru', etc.
    summary_json     TEXT NOT NULL,  -- Full summary JSON payload
    version          INTEGER DEFAULT 1,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (str, PK) - Summary ID
- `request_id` (str, FK, unique) - Foreign key to `requests`
- `lang` (str) - Summary language
- `summary_json` (str) - Full summary JSON (validated against contract)
- `version` (int) - Summary version (increments on regeneration)
- `created_at` (datetime) - Record creation timestamp

**Indexes:**

```sql
CREATE UNIQUE INDEX idx_summaries_request_id ON summaries(request_id);
CREATE INDEX idx_summaries_created_at ON summaries(created_at);
```

**Relationships:**

- One-to-one with `requests`
- One-to-one with `summary_embeddings`

---

## Search and Discovery Tables

### topic_search_index

**Purpose:** Postgres TSVECTOR + GIN full-text search index over the indexable surface of each request (title, body, tags). Replaces the historical SQLite FTS5 virtual table.

**Schema:**

```sql
CREATE TABLE topic_search_index (
    request_id    INTEGER PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
    url           TEXT,
    title         TEXT,
    snippet       TEXT,
    source        TEXT,
    published_at  TEXT,
    body          TEXT,
    tags          TEXT,
    body_tsv      TSVECTOR GENERATED ALWAYS AS (
                      to_tsvector(
                          'simple',
                          coalesce(title, '') || ' ' ||
                          coalesce(body,  '') || ' ' ||
                          coalesce(tags,  '')
                      )
                  ) STORED
);
CREATE INDEX ix_topic_search_body_tsv ON topic_search_index USING GIN (body_tsv);
```

**Fields:**

- `request_id` (int) — foreign key to `requests.id` (primary key)
- `url`, `title`, `snippet`, `source`, `published_at` — denormalised metadata copied from the source request
- `body`, `tags` — text fed into the tsvector
- `body_tsv` — generated `tsvector('simple', title || body || tags)` column with a GIN index

**Relationships:**

- One-to-one with `requests` (via `request_id`)

---

### summary_embeddings

**Purpose:** Postgres-side summary embedding metadata and drift tracking for semantic search. Qdrant stores the searchable vector point; the DB row records the model, content hash, index status, and last successful write.

**Schema:**

```sql
CREATE TABLE summary_embeddings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    summary_id        INTEGER UNIQUE REFERENCES summaries(id) ON DELETE CASCADE,
    model_name        TEXT NOT NULL,  -- 'all-MiniLM-L6-v2', etc.
    model_version     TEXT NOT NULL,
    embedding_blob    BLOB NOT NULL,  -- Serialized float32 array
    dimensions        INTEGER NOT NULL,  -- Vector dimensions (e.g., 384)
    language          TEXT,
    content_hash      TEXT,
    last_indexed_at   TIMESTAMP,
    index_status      TEXT NOT NULL DEFAULT 'pending',
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (int, PK, autoincrement) - Internal ID
- `summary_id` (int, FK, unique) - Foreign key to `summaries`
- `model_name` (str) - Model name used for embedding
- `model_version` (str) - Version tag for stale-vector detection
- `embedding_blob` (blob) - Serialized embedding vector
- `dimensions` (int) - Vector dimensions
- `language` (str, nullable) - Language hint for the embedded text
- `content_hash` (str, nullable) - SHA256 of the text fed to the embedding model
- `last_indexed_at` (datetime, nullable) - Last successful Qdrant write
- `index_status` (str, nullable) - Current index state (`indexed`, stale/error variants)
- `created_at` (datetime) - Record creation timestamp

**Indexes:**

```sql
CREATE UNIQUE INDEX idx_summary_embeddings_summary_id ON summary_embeddings(summary_id);
```

**Relationships:**

- One-to-one with `summaries`

---

## Mobile API Tables

### user_devices

**Purpose:** Track mobile devices for sync.

**Schema:**

```sql
CREATE TABLE user_devices (
    id                 TEXT PRIMARY KEY,
    user_id            INTEGER REFERENCES users(telegram_user_id),
    device_name        TEXT,
    device_type        TEXT,  -- 'ios', 'android', 'web'
    last_sync_token    TEXT,
    last_sync_at       TIMESTAMP,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (str, PK) - Device ID (UUID)
- `user_id` (int, FK) - Foreign key to `users`
- `device_name` (str, nullable) - Device name (user-defined)
- `device_type` (str, nullable) - Device type
- `last_sync_token` (str, nullable) - Last sync token
- `last_sync_at` (datetime, nullable) - Last sync timestamp
- `created_at` (datetime) - Record creation timestamp

**Indexes:**

```sql
CREATE INDEX idx_user_devices_user_id ON user_devices(user_id);
```

**Relationships:**

- Many-to-one with `users`

---

### refresh_tokens

**Purpose:** JWT refresh tokens for Mobile API.

**Schema:**

```sql
CREATE TABLE refresh_tokens (
    id              TEXT PRIMARY KEY,
    user_id         INTEGER REFERENCES users(telegram_user_id),
    token_hash      TEXT UNIQUE NOT NULL,
    device_id       TEXT,
    expires_at      TIMESTAMP NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    revoked         INTEGER DEFAULT 0,  -- 0/1 boolean
    revoked_at      TIMESTAMP
);
```

**Fields:**

- `id` (str, PK) - Token ID (UUID)
- `user_id` (int, FK) - Foreign key to `users`
- `token_hash` (str, unique) - SHA256 hash of refresh token
- `device_id` (str, nullable) - Associated device ID
- `expires_at` (datetime) - Token expiration timestamp
- `created_at` (datetime) - Token creation timestamp
- `revoked` (bool) - Revocation flag
- `revoked_at` (datetime, nullable) - Revocation timestamp

**Indexes:**

```sql
CREATE UNIQUE INDEX idx_refresh_tokens_token_hash ON refresh_tokens(token_hash);
CREATE INDEX idx_refresh_tokens_user_id ON refresh_tokens(user_id);
```

**Relationships:**

- Many-to-one with `users`

---

### collections

**Purpose:** User-created collections of summaries.

**Schema:**

```sql
CREATE TABLE collections (
    id              TEXT PRIMARY KEY,
    user_id         INTEGER REFERENCES users(telegram_user_id),
    name            TEXT NOT NULL,
    description     TEXT,
    icon            TEXT,
    color           TEXT,
    is_public       INTEGER DEFAULT 0,  -- 0/1 boolean
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (str, PK) - Collection ID (UUID)
- `user_id` (int, FK) - Foreign key to `users` (collection owner)
- `name` (str) - Collection name
- `description` (str, nullable) - Collection description
- `icon` (str, nullable) - Icon name/emoji
- `color` (str, nullable) - Color hex code
- `is_public` (bool) - Public visibility flag
- `created_at` (datetime) - Creation timestamp
- `updated_at` (datetime) - Last update timestamp

**Indexes:**

```sql
CREATE INDEX idx_collections_user_id ON collections(user_id);
```

**Relationships:**

- Many-to-one with `users`
- One-to-many with `collection_items`
- One-to-many with `collection_collaborators`

---

### collection_items

**Purpose:** Summaries within collections.

**Schema:**

```sql
CREATE TABLE collection_items (
    id              TEXT PRIMARY KEY,
    collection_id   TEXT REFERENCES collections(id) ON DELETE CASCADE,
    summary_id      TEXT REFERENCES summaries(id) ON DELETE CASCADE,
    position        INTEGER,
    notes           TEXT,
    added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (str, PK) - Item ID (UUID)
- `collection_id` (str, FK) - Foreign key to `collections`
- `summary_id` (str, FK) - Foreign key to `summaries`
- `position` (int, nullable) - Item position in collection
- `notes` (str, nullable) - User notes for this item
- `added_at` (datetime) - Timestamp when added to collection

**Indexes:**

```sql
CREATE INDEX idx_collection_items_collection_id ON collection_items(collection_id);
CREATE INDEX idx_collection_items_summary_id ON collection_items(summary_id);
CREATE UNIQUE INDEX idx_collection_items_unique ON collection_items(collection_id, summary_id);
```

**Relationships:**

- Many-to-one with `collections`
- Many-to-one with `summaries`

---

### collection_collaborators

**Purpose:** Users with access to shared collections.

**Schema:**

```sql
CREATE TABLE collection_collaborators (
    id              TEXT PRIMARY KEY,
    collection_id   TEXT REFERENCES collections(id) ON DELETE CASCADE,
    user_id         INTEGER REFERENCES users(telegram_user_id) ON DELETE CASCADE,
    role            TEXT DEFAULT 'viewer',  -- 'owner'| 'editor' |'viewer'
    added_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (str, PK) - Collaborator ID (UUID)
- `collection_id` (str, FK) - Foreign key to `collections`
- `user_id` (int, FK) - Foreign key to `users`
- `role` (str) - Access role
- `added_at` (datetime) - Timestamp when added as collaborator

**Indexes:**

```sql
CREATE INDEX idx_collection_collaborators_collection_id ON collection_collaborators(collection_id);
CREATE INDEX idx_collection_collaborators_user_id ON collection_collaborators(user_id);
CREATE UNIQUE INDEX idx_collection_collaborators_unique ON collection_collaborators(collection_id, user_id);
```

**Relationships:**

- Many-to-one with `collections`
- Many-to-one with `users`

---

### collection_invites

**Purpose:** Invite links for collection sharing.

**Schema:**

```sql
CREATE TABLE collection_invites (
    id              TEXT PRIMARY KEY,
    collection_id   TEXT REFERENCES collections(id) ON DELETE CASCADE,
    invite_code     TEXT UNIQUE NOT NULL,
    role            TEXT DEFAULT 'viewer',
    max_uses        INTEGER,
    uses_count      INTEGER DEFAULT 0,
    expires_at      TIMESTAMP,
    created_by      INTEGER REFERENCES users(telegram_user_id),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    revoked         INTEGER DEFAULT 0,  -- 0/1 boolean
    revoked_at      TIMESTAMP
);
```

**Fields:**

- `id` (str, PK) - Invite ID (UUID)
- `collection_id` (str, FK) - Foreign key to `collections`
- `invite_code` (str, unique) - Invite code (short UUID)
- `role` (str) - Role granted to invitees
- `max_uses` (int, nullable) - Maximum uses allowed
- `uses_count` (int) - Current use count
- `expires_at` (datetime, nullable) - Expiration timestamp
- `created_by` (int, FK) - Foreign key to `users` (invite creator)
- `created_at` (datetime) - Creation timestamp
- `revoked` (bool) - Revocation flag
- `revoked_at` (datetime, nullable) - Revocation timestamp

**Indexes:**

```sql
CREATE UNIQUE INDEX idx_collection_invites_invite_code ON collection_invites(invite_code);
CREATE INDEX idx_collection_invites_collection_id ON collection_invites(collection_id);
```

**Relationships:**

- Many-to-one with `collections`
- Many-to-one with `users` (via `created_by`)

---

## Audit and Analytics Tables

### user_interactions

**Purpose:** Track user actions for analytics.

**Schema:**

```sql
CREATE TABLE user_interactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER REFERENCES users(telegram_user_id),
    interaction_type TEXT NOT NULL,  -- 'request'| 'search' | 'collection_create' |etc.
    metadata_json   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Fields:**

- `id` (int, PK, autoincrement) - Internal ID
- `user_id` (int, FK) - Foreign key to `users`
- `interaction_type` (str) - Type of interaction
- `metadata_json` (str, nullable) - Interaction metadata as JSON
- `created_at` (datetime) - Interaction timestamp

**Indexes:**

```sql
CREATE INDEX idx_user_interactions_user_id ON user_interactions(user_id);
CREATE INDEX idx_user_interactions_created_at ON user_interactions(created_at);
```

**Relationships:**

- Many-to-one with `users`

---

### audit_logs

**Purpose:** System-wide audit trail.

**Schema:**

```sql
CREATE TABLE audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    level           TEXT NOT NULL,  -- 'info'| 'warning' |'error'
    event           TEXT NOT NULL,
    correlation_id  TEXT,
    user_id         INTEGER,
    details_json    TEXT
);
```

**Fields:**

- `id` (int, PK, autoincrement) - Internal ID
- `timestamp` (datetime) - Event timestamp
- `level` (str) - Log level
- `event` (str) - Event description
- `correlation_id` (str, nullable) - Request correlation ID
- `user_id` (int, nullable) - Associated user ID
- `details_json` (str, nullable) - Event details as JSON

**Indexes:**

```sql
CREATE INDEX idx_audit_logs_timestamp ON audit_logs(timestamp);
CREATE INDEX idx_audit_logs_correlation_id ON audit_logs(correlation_id);
CREATE INDEX idx_audit_logs_user_id ON audit_logs(user_id);
```

---

## Client Secrets (Mobile API)

### client_secrets

**Purpose:** Mobile API client credentials.

**Schema:**

```sql
CREATE TABLE client_secrets (
    id                TEXT PRIMARY KEY,
    client_id         TEXT UNIQUE NOT NULL,
    client_secret_hash TEXT NOT NULL,
    client_name       TEXT,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    revoked           INTEGER DEFAULT 0,  -- 0/1 boolean
    revoked_at        TIMESTAMP
);
```

**Fields:**

- `id` (str, PK) - Secret ID (UUID)
- `client_id` (str, unique) - Client identifier
- `client_secret_hash` (str) - SHA256 hash of client secret
- `client_name` (str, nullable) - Client application name
- `created_at` (datetime) - Creation timestamp
- `revoked` (bool) - Revocation flag
- `revoked_at` (datetime, nullable) - Revocation timestamp

**Indexes:**

```sql
CREATE UNIQUE INDEX idx_client_secrets_client_id ON client_secrets(client_id);
```

---

## Entity Relationship Diagram

```mermaid
erDiagram
    users | |--o{ requests : "submits"
    users | |--o{ user_interactions : "performs"
    users | |--o{ user_devices : "owns"
    users | |--o{ refresh_tokens : "has"
    users | |--o{ collections : "creates"
    users | |--o{ collection_collaborators : "collaborates"

    chats | |--o{ requests : "contains"
    chats | |--o{ telegram_messages : "has"

    requests | | -- | | telegram_messages : "has"
    requests | | -- | | crawl_results : "has"
    requests | | -- | | video_downloads : "has"
    requests | |--o{ llm_calls : "triggers"
    requests | | -- | | summaries : "produces"

    summaries | | -- | | summary_embeddings : "has"
    summaries | | -- | | topic_search_index : "indexed_in"
    summaries | |--o{ collection_items : "included_in"

    collections | |--o{ collection_items : "contains"
    collections | |--o{ collection_collaborators : "shared_with"
    collections | |--o{ collection_invites : "has"
```

---

## Common Queries

### Find All Summaries for a User

```sql
SELECT s.*
FROM summaries s
JOIN requests r ON s.request_id = r.id
WHERE r.user_id = 123456789
ORDER BY s.created_at DESC
LIMIT 10;
```

### Find Correlation ID from Telegram Message ID

```sql
SELECT r.id as correlation_id
FROM requests r
JOIN telegram_messages tm ON r.id = tm.request_id
WHERE tm.message_id = 12345;
```

### Calculate Total Token Usage by Model

```sql
SELECT
    model,
    COUNT(*) as calls,
    SUM(total_tokens) as total_tokens,
    AVG(total_tokens) as avg_tokens,
    SUM(cost_usd) as total_cost_usd
FROM llm_calls
WHERE created_at > now() - interval '30 days'
GROUP BY model
ORDER BY total_tokens DESC;
```

### Find Slow Requests

```sql
SELECT
    r.id,
    r.input_url,
    r.total_processing_time_sec
FROM requests r
WHERE r.total_processing_time_sec > 15
ORDER BY r.total_processing_time_sec DESC
LIMIT 10;
```

---

## Database Maintenance

### Vacuum (Reclaim Space)

```bash
psql "$DATABASE_URL" -c "VACUUM;"
```

### Analyze (Update Query Planner Statistics)

```bash
psql "$DATABASE_URL" -c "ANALYZE;"
```

### Integrity Check

```bash
python -m app.cli.healthcheck
```

---

## Mixed-Source Aggregation: Source Model

`SourceKind` is the shared source taxonomy for bundle items: `x_post`, `x_article`, `threads_post`, `instagram_post`, `instagram_carousel`, `instagram_reel`, `web_article`, `telegram_post`, `telegram_post_with_images`, `telegram_album`, and `youtube_video`.

`SourceItem` is the normalized source identity object. URL-backed items dedupe on normalized URL unless a stronger platform identifier is available (`external_id` such as a tweet ID or YouTube video ID). Telegram-native items dedupe on `(chat_id, message_id)` or `(chat_id, media_group_id)`.

`NormalizedSourceDocument` is the extractor output contract: `{source_item_id, source_kind, title?, text, detected_language?, text_blocks[], media[], metadata{}, provenance{...}}`.

Failures are stored at two levels: bundle-level on `aggregation_sessions` and item-level on `aggregation_session_items`, both using `failure_code`, `failure_message`, and JSON `failure_details`.

**Rollout flags:**

- `AGGREGATION_BUNDLE_ENABLED=false` disables the surface entirely.
- `AGGREGATION_ROLLOUT_STAGE` supports `disabled`, `internal`, `owner_beta`, and `enabled`.
- `AGGREGATION_META_EXTRACTORS_ENABLED=false` disables dedicated Threads/Instagram extraction while leaving bundle orchestration enabled.
- `AGGREGATION_ARTICLE_MEDIA_ENABLED=false` disables multimodal article/X image propagation while leaving text extraction intact.
- `AGGREGATION_NON_YOUTUBE_VIDEO_ENABLED=false` disables shared Telegram/Meta video normalization.

---

## Database Migrations

**Alembic** (`app/db/alembic/`) is the authoritative schema migration system.

```bash
# Apply all pending migrations
alembic upgrade head
# Equivalent convenience wrapper
python -m app.cli.migrate_db
```

Alembic revision files live in `app/db/alembic/versions/`. The PostgreSQL baseline is the authoritative DDL source for live databases. Legacy SQLite revision snapshots are retained under `app/db/alembic/versions/_legacy_sqlite/` only for migration archaeology and must not be applied to PostgreSQL.

Startup schema changes are not performed by application code; run Alembic before starting services.

---

## URL Normalization and Deduplication

Every URL submitted to the pipeline is normalized before storage and deduplication:

- Lowercase scheme and host.
- Strip fragment (`#…`).
- Sort query parameters alphabetically.
- Remove known tracking parameters (configurable list).
- Collapse trailing slash.

The normalized URL is hashed: `sha256(normalized_url)` → `requests.dedupe_hash` (unique index). If a repeat is seen, the existing `crawl_results` row is reused unless `--force` is passed. This ensures the same article submitted multiple times produces exactly one crawl and one LLM call.

Source: `app/core/url_utils.py`.

---

## GitHub Repository Tables

Three tables added in the GitHub repository ingestion feature. SQLAlchemy models: `app/db/models/repository.py`.

### repositories

**Purpose:** One row per ingested GitHub repository per user. Stores metadata fetched from the GitHub API and the LLM-generated analysis fields.

**Key columns:**

| Column | Type | Notes |
|--------|------|-------|
| `id` | integer PK | Surrogate key |
| `github_id` | bigint | GitHub's numeric repo ID (unique per user) |
| `owner` | text | Repository owner |
| `name` | text | Repository name |
| `full_name` | text | `owner/repo` |
| `url` | text | `https://github.com/owner/repo` |
| `homepage_url` | text, nullable | Project homepage |
| `description` | text, nullable | GitHub description |
| `primary_language` | text, nullable | Primary language |
| `languages_json` | JSON object | GitHub language byte counts |
| `topics_json` | JSON array | GitHub topic tags |
| `stars` | int | Stars at last sync |
| `forks` | int | Fork count |
| `watchers` | int | Watcher count |
| `default_branch` | text, nullable | Default branch name |
| `license_spdx` | text, nullable | SPDX license identifier |
| `is_archived` | bool | GitHub archived flag |
| `is_fork` | bool | GitHub fork flag |
| `is_template` | bool | GitHub template flag |
| `pushed_at` | timestamp, nullable | Last GitHub push time |
| `created_at_github` | timestamp, nullable | GitHub repo creation time |
| `readme_excerpt` | text, nullable | Truncated README text used for analysis |
| `readme_etag` | text, nullable | README ETag |
| `analysis_json` | JSON, nullable | `RepoAnalysis` structured output |
| `analysis_model` | text, nullable | Model used for analysis |
| `analysis_at` | timestamp, nullable | When analysis was generated |
| `content_hash` | text, nullable | Hash of readme + description; change triggers reanalysis |
| `source` | text | `manual` or `starred` |
| `is_starred` | bool | Whether in the user's starred list |
| `user_id` | bigint FK -> users | Owner of the integration |
| `last_synced_at` | timestamp, nullable | Last GitHub API fetch |
| `pending_analysis` | bool | LLM analysis deferred by budget cap |
| `created_at` | timestamp | Row creation |
| `updated_at` | timestamp | |

Unique constraint: `(user_id, github_id)`.

Indexes: `(user_id, is_starred)`, `(user_id, primary_language)`, `(user_id, pushed_at DESC)`, `(github_id)`.

### repository_embeddings

**Purpose:** Vector embeddings for `repositories` rows, used by `GET /v1/search/repositories`. Qdrant writes use deterministic repository point IDs shared by the fast path and CocoIndex (`app/infrastructure/vector/point_ids.py`).

**Key columns:**

| Column | Type | Notes |
|--------|------|-------|
| `id` | integer PK | Surrogate key |
| `repository_id` | integer FK -> repositories | One-to-one, ON DELETE CASCADE |
| `model_name` | text | Model identifier |
| `model_version` | text | Version tag; stale rows are targets for `backfill_repository_embeddings` |
| `embedding_blob` | binary | Serialized float32 array |
| `dimensions` | int | Vector dimensions |
| `language` | text, nullable | Language hint for the embedded text |
| `created_at` | timestamp | |

Unique constraint: `(repository_id)`.

### user_github_integrations

**Purpose:** Per-user GitHub OAuth or PAT integration record. The encrypted token is stored here; the encryption key (`GITHUB_TOKEN_ENCRYPTION_KEY`) is required at runtime. Losing the key renders existing tokens unreadable -- see the MultiFernet rotation hint in `app/security/secret_crypto.py`. `app/security/token_crypto.py` remains the backward-compatible GitHub import facade.

**Key columns:**

| Column | Type | Notes |
|--------|------|-------|
| `id` | integer PK | Surrogate key |
| `user_id` | bigint FK -> users | Unique per user |
| `auth_method` | text | `pat` or `oauth_device` |
| `encrypted_token` | binary | Fernet-encrypted access token |
| `token_scopes` | text, nullable | OAuth scopes granted |
| `github_login` | text, nullable | GitHub username |
| `github_user_id` | bigint, nullable | GitHub numeric user ID |
| `status` | text | `active`, `needs_reauth`, `revoked` |
| `last_synced_at` | timestamp, nullable | Most recent sync completion |
| `last_sync_cursor` | text, nullable | Reserved pagination cursor |
| `last_full_sync_at` | timestamp, nullable | Most recent full-pagination sync |
| `notified_needs_reauth_at` | timestamp, nullable | Last reauth notification time |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

Unique constraint: `(user_id)`.

### social_connections

**Purpose:** Generic encrypted social provider credential storage for X, Instagram, and Threads. OAuth and connection-status endpoints use this table to expose safe provider/account status while keeping access and refresh tokens as Fernet ciphertext bytes produced through `app/security/secret_crypto.py`; token bytes and source payloads must not be emitted in logs, audit payloads, diagnostics, or JSON responses.

**Key columns:**

| Column | Type | Notes |
|--------|------|-------|
| `id` | integer PK | Surrogate key |
| `user_id` | bigint FK -> users | Owner of the connection |
| `provider` | enum | `x`, `instagram`, or `threads` |
| `auth_type` | enum | `oauth2`, `cookie`, or `manual` |
| `provider_user_id` | text, nullable | Provider account identifier |
| `provider_username` | text, nullable | Provider handle/display username |
| `encrypted_access_token` | binary, nullable | Fernet-encrypted access token or equivalent secret |
| `encrypted_refresh_token` | binary, nullable | Fernet-encrypted refresh token when available |
| `token_scopes` | jsonb, nullable | Granted scopes as a string array |
| `access_token_expires_at` | timestamp, nullable | Access token expiration |
| `refresh_token_expires_at` | timestamp, nullable | Refresh token expiration |
| `last_used_at` | timestamp, nullable | Last successful or attempted provider use recorded by social workflows |
| `status` | enum | `active`, `needs_reauth`, `revoked`, or `disabled` |
| `metadata_json` | jsonb, nullable | Non-secret provider metadata |
| `created_at` | timestamp | Row creation |
| `updated_at` | timestamp | Last update |

Unique constraint: `(user_id, provider)`.

Indexes: `(user_id, status)`, `(provider, provider_user_id)`.

### social_auth_states

**Purpose:** OAuth state persistence for social providers. The table stores hashed state values and optional encrypted PKCE verifier bytes so authorization callbacks can validate state without introducing raw verifier storage.

**Key columns:** `user_id`, `provider`, `state_hash`, `encrypted_code_verifier`, `redirect_uri`, `scopes`, `status`, `metadata_json`, `expires_at`, `consumed_at`, `created_at`.

Unique constraint: `(provider, state_hash)`.

Indexes: `(user_id, provider)`, `(expires_at)`.

### social_fetch_attempts

**Purpose:** Fetch/sync attempt audit trail for social connections. Extraction and diagnostics use this table as a non-secret place to record provider, attempt type, status, timing, error code/message, and sanitized metadata.

**Key columns:** `connection_id`, `user_id`, `provider`, `attempt_type`, `status`, `started_at`, `finished_at`, `error_code`, `error_message`, `metadata_json`, `created_at`.

Indexes: `(connection_id, started_at)`, `(user_id, provider)`.

---

## See Also

- [SPEC.md](../SPEC.md) - Navigation index
- [CLI Commands § Database Migration](cli-commands.md#database-migration) - Migration tool
- [How to Backup and Restore](../guides/backup-and-restore.md) - Backup procedures

---

**Last Updated:** 2026-05-23
