# X Bookmarks Integration

How Ratatoskr adopts the private `fieldtheory-cli` tool (`ft`) as a first-class content source: bookmark ingestion as a peer source kind, MCP search backed by Postgres FTS, semantic indexing of the wiki corpus, Telegram-surface access to "Possible run" idea output, and a Claude Code skill for agent integration.

**Audience:** Contributors implementing the integration (Steps 2–7 of the rollout), operators wiring up host-side `ft` scheduling, integrators querying the bookmark corpus via MCP. **Type:** Explanation. **Related:** [`docs/SPEC.md`](../SPEC.md) (navigation hub), [`docs/explanation/architecture-overview.md`](architecture-overview.md) (subsystem index), [`docs/explanation/github-repository-ingestion.md`](github-repository-ingestion.md) (peer ingestion subsystem), [`docs/explanation/scraper-chain.md`](scraper-chain.md) (the chain this subsystem deliberately bypasses), [`docs/reference/data-model.md`](../reference/data-model.md) (canonical schema).

---

## Overview

`ft` is a local-first X/Twitter bookmark sync tool: it pulls the operator's bookmarks (via an authenticated browser session captured on the host), classifies each bookmark with an LLM into one of seven categories (`tool`, `security`, `technique`, `launch`, `research`, `opinion`, `commerce`), maintains an FTS5-indexed SQLite store at `~/.fieldtheory/bookmarks.db`, exports topic-aggregated wiki pages to `~/.fieldtheory/library/*.md`, and periodically runs idea-generation passes that produce structured JSON in `~/.fieldtheory/ideas/`.

The integration treats `ft` as a **discovery and pre-extraction tier that runs adjacent to Ratatoskr, not inside it**. Bookmarks become a peer source alongside Twitter forwards, GitHub stars, and RSS items: ingested into Postgres, surfaced via MCP, semantically indexed in Qdrant via the wiki corpus, and exposed to the operator's Telegram interface for idea retrieval. No `Summary` rows are generated for bookmarks on the default path — `ft`'s pre-extracted tweet text is the canonical representation. The LLM-heavy LangGraph summarize/repair loop is reserved for user-forwarded URLs; bookmark ingestion costs zero LLM tokens per sync.

The six integration areas — bookmark ingestor, MCP search tool, wiki indexer, classification metadata, `/x_possible` Telegram command, and Claude Code skill registration — share a single host-side `ft` binary and a single read-only mount of `~/.fieldtheory/` into the container fleet.

---

## Container Topology

`ft` is a Node + Playwright + Chromium toolchain. It is **never installed inside the Ratatoskr container images**. Instead:

- The host runs `ft sync`, `ft possible run`, `ft auth`, and `ft skill install` via OS-native scheduling (launchd on macOS, systemd user units on Linux, cron on the Pi). The planned host setup reference is listed in Step 7 below.
- `~/.fieldtheory/` is bind-mounted **read-only** as `/x_bookmarks:ro` into the `worker`, `ratatoskr` bot, and `mcp` / `mcp-write` containers. It is **not** mounted into `mobile-api` — the mobile API reads through Postgres + Qdrant, which the worker already populates.
- Container code reads `bookmarks.db` via `aiosqlite` in read-only mode (`?mode=ro` URI flag), walks `library/*.md` via `pathlib`, and reads the newest JSON file in `ideas/`. **The container performs zero `ft` subprocess invocations.** Operating Rule 6's `asyncio.to_thread(subprocess.run, ["ft", ...])` guidance is vacuously satisfied — no such call exists in the container.

```
host
  +-- ft sync           (hourly via launchd/systemd/cron)
  +-- ft possible run   (manual, when operator wants fresh ideas)
  +-- ~/.fieldtheory/
        |-- bookmarks.db        (single writer: ft)
        |-- bookmarks.jsonl     (append-only audit, ft-managed)
        |-- library/*.md        (wiki output, ft-managed)
        +-- ideas/*.json        (Possible run output, ft-managed)
                  |
                  | (bind-mount, read-only)
                  v
container fleet
  worker      reads bookmarks.db deltas, walks library/ for wiki sync
  ratatoskr   reads ideas/ for /x_possible
  mcp(-write) reads via Postgres (NOT bookmarks.db) for ft_search
  mobile-api  reads via Postgres + Qdrant only (no mount)
```

**Concurrency safety.** Single writer (host `ft`), multiple readers (container Taskiq tasks). SQLite WAL mode handles this cleanly; the `?mode=ro` URI flag prevents accidental write-lock acquisition from the container side. The reconciliation cadence (15-minute delta scan, hourly wiki walk) is loose enough that "the file is mid-write" windows do not contend.

**Pi deployment.** Two supported modes for the host-side `ft` binary: (a) Pi runs `ft` directly (Node + npm + a one-time `ft auth` flow over VNC/X11), or (b) Mac runs `ft` and rsyncs `~/.fieldtheory/` to the Pi on the same cadence as `ft sync`. Both modes converge on the same container code paths. The choice is documented at host-setup time.

**Trade-off accepted.** A host wrapper daemon brokering `ft` invocations into the container would enable on-demand `/x_bookmarks_sync` from Telegram, but adds an indirection layer without solving a v1 problem. Cron-driven `ft sync` plus a Unix-socket nudge from the bot (deferred to v2) covers the same use case. Reverting to "ft inside the container" is a one-time `docker-compose.yml` revision plus image rebuild — no data migration, no irreversible commitment.

---

## Data Model

### `x_bookmark_metadata` (new join table)

`ft`'s bookmark identity is mirrored into Postgres as a sibling row on the existing `requests` table. The bookmark URL flows through `app/core/url_utils.py` to produce the normalized `dedupe_hash`, which keys the `requests` row in the usual way. The x_bookmarks-specific metadata lives in a join table keyed by `request_id`:

| Column | Type | Notes |
|---|---|---|
| `request_id` | UUID PK, FK → `requests.id` `ON DELETE CASCADE` | Bookmark and request share lifecycle. |
| `bookmark_external_id` | TEXT NOT NULL UNIQUE | `ft`'s internal bookmark id (matches `bookmarks.db.id`). |
| `x_category` | TEXT NOT NULL | CHECK constraint enum: `tool`, `security`, `technique`, `launch`, `research`, `opinion`, `commerce`. |
| `tweet_text` | TEXT | The bookmark's extracted tweet body. Nullable for non-tweet bookmarks (long-form articles, threads collapsed to root). |
| `tweet_text_tsv` | TSVECTOR | GENERATED ALWAYS AS `to_tsvector('english', coalesce(tweet_text, ''))` STORED. GIN-indexed. |
| `tweet_author` | TEXT | The handle that authored the tweet (e.g., `@karpathy`). |
| `tweet_url` | TEXT NOT NULL | Canonical bookmark URL (post-`url_utils` normalization). |
| `posted_at` | TIMESTAMPTZ | Timestamp the bookmarked content was posted (not when it was bookmarked). Nullable when unknown. |
| `synced_at` | TIMESTAMPTZ NOT NULL | Timestamp this row was last refreshed from `bookmarks.db`. |

**Indexes:**

- `ix_x_bookmark_metadata_bookmark_external_id` (UNIQUE) — lookup by `ft` id during delta sync.
- `ix_x_bookmark_metadata_category` — filter by classification in MCP queries.
- `ix_x_bookmark_metadata_tweet_text_tsv` USING GIN — FTS ranking for `x_search`.

**No lifecycle columns.** No `unbookmarked_at`, no `last_observed_at`, no `deleted_at`, no `is_active`. The schema reflects the lifecycle policy below: bookmarks are immortal once ingested, so the schema has no field to express "no longer bookmarked." Adding a `currently_bookmarked` boolean is schema-additive with no migration cost; it is explicitly deferred to v2 once a concrete read-path use case justifies maintaining it.

### Source-kind discriminator

`SourceKind.X_BOOKMARK` is the new system-level discriminator surfaced to the mobile API. It signals to the rest of the pipeline:

- This row was ingested via the bookmark sync path, not the scraper chain.
- A bookmark sibling row exists in `x_bookmark_metadata`.
- The standard `crawl_results` → `llm_calls` → `summaries` chain did not run; consumers should fall back to `tweet_text` when querying for representative content.

### Why mirror `tweet_text` into Postgres instead of querying SQLite

`ft`'s `bookmarks.db` already carries the tweet body. Mirroring it into Postgres at sync time costs ~280 bytes per bookmark (mean tweet length) but collapses three otherwise expensive properties:

1. **MCP search becomes a single Postgres query.** `ts_rank_cd` over `tweet_text_tsv`, joined to `requests` for canonical URL and timestamps. No `aiosqlite` cross-process read, no FTS5 ↔ tsvector ranking mismatch, no transactional gap between Postgres state and the search result.
2. **Read consistency.** Mobile API endpoints (future), the MCP tool, and any future Telegram surface all read from the same Postgres row. No "the bookmark is in MCP but not in the API" inconsistency window.
3. **Backup parity.** Postgres dumps include the bookmark corpus. Pi snapshot recovery does not depend on the host's `~/.fieldtheory/` being intact.

The trade-off is one column-store of duplicated text. At a thousands-of-bookmarks scale, this is on the order of a megabyte — negligible against Postgres's existing summary text store.

---

## Ingestion Path (Area 1)

Bookmark ingestion is a peer to the existing source ingestors in `app/adapters/ingestors/` (`HackerNewsIngester`, `RedditIngester`, `ThreadsUserThreadsIngester`, `XTimelineIngester`). It deliberately does **not** route through `URLProcessor`, the scraper chain, or the LangGraph summarize/repair loop.

```
~/.fieldtheory/bookmarks.db                (single writer: ft)
  |
  | aiosqlite ?mode=ro (read-only)
  v
XBookmarksIngestor              (app/adapters/ingestors/x_bookmarks_ingestor.py)
  |-- delta-scan: SELECT * FROM bookmarks WHERE synced_at > :last_seen
  |-- for each bookmark:
  |     |-- normalize_url(bookmark.url) -> canonical_url, dedupe_hash
  |     |-- look up requests row by dedupe_hash
  |     |-- MISS: insert requests row (source_kind=X_BOOKMARK,
  |     |        processing_status=x_imported, correlation_id=<uuid4>)
  |     |        insert x_bookmark_metadata row
  |     |-- HIT:  upsert x_bookmark_metadata row only
  |     |        (request is already tracked; bookmark adds the metadata sidecar)
  |     |-- emit ingestor metric (counter: by category, by miss/hit)
  v
Postgres state in sync with bookmarks.db deltas
```

The ingestor's contract:

- **Idempotent.** Re-running over the same `bookmarks.db` is a no-op; the `synced_at` watermark advances only when new rows are produced.
- **Correlation IDs.** Every newly-created `requests` row gets a fresh `correlation_id`; every persisted artifact (errors, audit entries) carries it. Existing requests retain their original correlation ID — bookmark sync does not clobber prior history.
- **No URL processor invocation.** A bookmark URL that was previously processed via the scraper chain (e.g., the operator forwarded the same tweet earlier) keeps its `Summary` and `Summary.text`. The bookmark sync only adds the metadata sidecar. A bookmark URL that is brand-new gets a `requests` row in `processing_status=x_imported` and never enters the scraper chain.
- **Cost.** Per-sync LLM cost = 0. Per-sync Firecrawl/Playwright cost = 0. The work is dominated by SQLite reads (~10k bookmarks scans in milliseconds) and Postgres upserts.

**Rate-limit isolation.** The Twitter adapter never fires from the ingestor lane. User-forwarded URLs continue to consume the full Telethon/Firecrawl/Playwright rate budget; bookmark ingestion does not contend with it. This is a load-bearing property.

**Deferred v2 path: `/x_promote <bookmark>`.** A future operator-triggered command that transitions a `requests` row from `processing_status=x_imported` to `pending` and hands it to the URL processor for full summarization. The data model supports this transition without schema change — only the `processing_status` enum needs the new value. The v1 read paths (MCP, wiki, `/x_possible`) cover the corpus without per-bookmark `Summary` rows, so the promote flow is explicitly deferred until a concrete use case demands it.

---

## Lifecycle Policy

### Bookmarks: immortal once ingested

When the operator unbookmarks a tweet on X, `ft` eventually drops it from `bookmarks.db` (its folder-tag set becomes empty). The ratatoskr ingestor **does not propagate this deletion**. Once a `requests` + `x_bookmark_metadata` pair is written, it persists.

**Why.** `ft`'s disappearance signal is intrinsically noisy:

- `ft` re-walks folder-tag membership periodically; a bookmark that drops off in one walk and reappears in the next is indistinguishable from a deliberate unbookmark.
- `ft sync` may legitimately produce an empty folder-tag set if the X API rate-limits the walker mid-fetch.
- The cost of a false-positive "unbookmark" is permanent loss of a `summaries` row (if Area 1's deferred `/x_promote` ever ran on this row) and the operator's only record of having seen the content.

The cost of "immortal bookmarks" is bounded: a thousands-of-bookmarks corpus consumes ~1 MB of `tweet_text`. The cost of "purge on disappearance" is unbounded recoverability loss. The default is therefore "never delete."

**Operator escape valve.** Manual `DELETE FROM requests WHERE id = ...` removes the bookmark and its metadata via `ON DELETE CASCADE`. This is the same gesture used elsewhere in the codebase for one-off cleanup.

### Wiki files: hard-delete on filesystem disappearance

By contrast, the wiki indexer (Area 3) **does** hard-delete from Qdrant when a `library/*.md` file disappears. The disappearance signal is reliable here: `ft wiki` regenerates the library from scratch on every run, so an absent file means `ft` no longer believes that topic warrants a wiki page. The Qdrant index reflects current `ft` state; orphan vectors are pruned on each indexer pass.

The asymmetry (bookmarks immortal, wiki ephemeral) is deliberate. Bookmarks are operator gestures with intrinsic provenance value; wiki pages are derived aggregations that `ft` owns end-to-end.

---

## Cadence

Four independent cadence axes, each tunable via an env var with a default that matches Ratatoskr's existing scheduling idioms (`GITHUB_SYNC_CRON`, `VECTOR_RECONCILE_CRON`, `RETENTION_CRON`):

| Axis | Where it runs | Default | Env var | Purpose |
|---|---|---|---|---|
| Host bookmark sync | host | hourly | (host-side scheduling unit, not a ratatoskr env var) | `ft sync` pulls new bookmarks from X into `bookmarks.db`. |
| Host idea generation | host | manual | (host-side, no schedule) | `ft possible run` regenerates `ideas/*.json`. LLM-expensive; operator-triggered. |
| Container bookmark delta-scan | worker | every 15 minutes | `X_BOOKMARKS_SYNC_CRON='*/15 * * * *'` | `XBookmarksIngestor` reads `bookmarks.db` deltas, writes Postgres. |
| Container wiki walk | worker | hourly | `X_WIKI_SYNC_CRON='0 * * * *'` | `XWikiSync` walks `library/*.md`, syncs Qdrant. |

**Asymmetric pairing rationale.**

- Host sync (hourly) ⟷ container delta-scan (15 min): the container picks up scheduled host syncs *and* manual host gestures (e.g., the operator runs `ft sync` ad hoc; the container reconciles within 15 minutes). Asymmetric on purpose.
- Host idea generation (manual) ⟷ `/x_possible` handler (reactive): the handler reads the newest `ideas/*.json` on user gesture; cadence coupling is N/A. If no ideas file exists, the handler returns a friendly fallback (see Telegram surface below).
- Container wiki walk (hourly) runs even when `ft wiki` has not regenerated the library. This is acceptable: the cost is dominated by `stat()` (cheap), with content-hash comparison only running on changed files.

**Host setup.** Sample launchd `.plist` and systemd `.service` / `.timer` units live in `ratatoskr/ops/host-units/` (created in Step 7). The host-setup reference doc walks through both modes (direct Pi-side `ft`, or Mac-side `ft` + rsync to Pi).

---

## MCP Search Tool (Area 2)

`x_search(query: str, category?: str, limit?: int) -> SearchResult[]` is registered in `app/mcp/server.py` alongside the existing tools (`search_summaries`, `get_summary`, `search_repositories`, ...).

The implementation is a Postgres query, **not** a subprocess call to `ft search`:

```sql
SELECT
  r.id, r.canonical_url,
  m.x_category, m.tweet_text, m.tweet_author, m.posted_at,
  ts_rank_cd(m.tweet_text_tsv, plainto_tsquery('english', :query)) AS rank
FROM x_bookmark_metadata m
JOIN requests r ON r.id = m.request_id
WHERE m.tweet_text_tsv @@ plainto_tsquery('english', :query)
  AND (:category IS NULL OR m.x_category = :category)
ORDER BY rank DESC, m.posted_at DESC NULLS LAST
LIMIT :limit;
```

Return shape (JSON):

```json
{
  "results": [
    {
      "request_id": "...",
      "canonical_url": "https://x.com/...",
      "category": "research",
      "tweet_text": "...",
      "tweet_author": "@karpathy",
      "posted_at": "2026-04-12T...",
      "rank": 0.0834
    }
  ]
}
```

**Why Postgres FTS instead of `ft search` subprocess.** The mirrored `tweet_text` and `tweet_text_tsv` columns make `ts_rank_cd` a direct Postgres operation. Ranking quality on tweet-length text matches FTS5 BM25 closely; zero subprocess overhead; consistent transaction boundary; no concurrency contention with the host-side `ft` writer; no Node toolchain in the container. The original PROMPT.md guidance to call `ft` via `asyncio.to_thread(subprocess.run, ...)` is dropped — there is no subprocess call to wrap.

**Reversibility.** If FTS ranking quality proves insufficient (e.g., the operator wants `ft`'s LLM-augmented relevance), the tool body swaps to `aiosqlite` reading `bookmarks_fts` from `~/.fieldtheory/bookmarks.db` (the `/x_bookmarks:ro` mount is already in place). No schema change.

---

## Wiki Indexer (Area 3)

`ft wiki` produces topic-aggregated markdown files at `~/.fieldtheory/library/*.md`. Each file collates multiple bookmarks under a topical heading (e.g., `library/transformers.md`, `library/agent-evals.md`). These are the semantic-vector surface for the bookmark corpus.

The `XWikiSync` Taskiq task (hourly) walks `library/` and reconciles its content into Qdrant via the existing `VectorIndexedEntityAdapter` pattern:

```
~/.fieldtheory/library/*.md
  |
  | pathlib walk
  v
XWikiSync                  (app/tasks/x_wiki_sync.py)
  |-- for each *.md:
  |     |-- compute content_hash (sha256 of file body)
  |     |-- look up by stable point_id = sha256(file path)
  |     |   (path is the natural key; content_hash drives the re-embed decision)
  |     |-- changed?  re-embed via EmbeddingFactory (sentence-transformers, Gemini, or Voyage)
  |     |             upsert Qdrant point (entity_type="x_wiki")
  |     |-- unchanged? skip (cheap stat() pass)
  |-- compute set difference: known_paths - current_paths
  |-- for each orphan: Qdrant delete by point_id (hard-delete on FS disappearance)
```

Wiki vectors share the Qdrant collection with summaries and repositories, distinguished by `entity_type="x_wiki"`. Semantic search routes that span "show me my bookmarks and my summaries on transformers" work without query-side fan-out.

**No `Summary` row per wiki page.** The wiki is a derived view of the bookmark corpus, not a per-page summary. It does not enter the `summaries` table; its only persistence beyond the source filesystem is the Qdrant vector.

---

## Telegram Surface: `/x_possible` (Area 5)

The handler is a vanilla Telegram command registered through `app/adapters/telegram/command_handlers/x_possible.py` via the `CommandRegistry` pattern (see the `adding-telegram-command` skill).

**Auth.** No new env var, no new gate. The command inherits the existing `ALLOWED_USER_IDS` whitelist via the central `AccessController.check_access` call that runs before command dispatch in `app/adapters/telegram/access_controller.py`. The handler body does **not** call `check_access` explicitly — it is a registered command, so the router gates it upstream. This matches every other command in the codebase (admin, aggregation, backup, content, digest, export, init_session, listen, onboarding, rss, rules, search, settings, social).

Why `ALLOWED_USER_IDS` is the right gate: ratatoskr is documented as a single-tenant Telegram bot with an owner-only whitelist; the whitelist is non-optional at startup (`AccessController.__init__` raises `RuntimeError` if `allowed_user_ids` is empty), so there is no "open by default" footgun. Adding a separate `X_POSSIBLE_USER_IDS` env var would solve a multi-tenant delegation problem that this product does not have.

**Handler flow.**

```
/x_possible
  |
  v
XPossibleHandler                (app/adapters/telegram/command_handlers/x_possible.py)
  |-- receive correlation_id from router
  |-- list ~/.fieldtheory/ideas/*.json sorted by mtime descending
  |-- if no files: reply with EN/RU fallback
  |   "No ideas yet. Run `ft possible run` on the host to generate ideas first; the bot only displays existing ideas, it does not generate them."
  |-- else: read newest file, parse, format top-N idea nodes as a Telegram message
  |-- on parse failure: persist error to DB with correlation_id intact;
  |                     reply "Could not read ideas file. Error ID: <correlation_id>"
```

**No subprocess call to `ft possible run`.** v1 is read-only. The operator triggers idea generation on the host (`ft possible run --defaults --background`); the handler only displays results. This matches the manual P3 cadence axis above. A future v2 may wire a host-side daemon wrapper that the handler nudges via Unix socket; the data model and command surface accommodate this without change.

**Prompt strings.** Both `en` and `ru` mirrored pairs must be added in `app/prompts/` and `app/adapters/telegram/command_handlers/` help text, per Operating Rule 7. Changing one without the other silently breaks the other-language path.

---

## Claude Code Skill (Area 6)

`ft skill install` registers the `/fieldtheory` skill in `.claude/skills/x_bookmarks/` (managed by `ft`, not by ratatoskr). The skill exposes `ft search`, `ft sync status`, and `ft possible run` to Claude Code agents working in the workspace.

Workspace documentation (`CLAUDE.md` at the workspace root, and `ratatoskr/CLAUDE.md`) is updated in Step 7 to note:

- The skill is available and how to invoke it.
- `ft sync` should be run before starting an agent session that relies on bookmark context.
- The host-setup reference planned for `docs/reference/x-bookmarks-host-setup.md` covers the launchd/systemd/cron configuration.

The skill itself is not modified by ratatoskr; `ft skill install` is the integration boundary.

---

## Implementation Outline (Steps 2–6 sketches)

This section is a roadmap, not full code. Each step lands as a single conventional commit with `make format && make lint && make type` as the quality gate.

### Step 2: Bookmark ingestor (Area 1)

- `app/adapters/ingestors/x_bookmarks_ingestor.py` — `XBookmarksIngestor` class; `aiosqlite` read-only connection; delta-scan by `synced_at` watermark; upsert via `Database`.
- `app/tasks/x_bookmarks_sync.py` — Taskiq cron task `ratatoskr.x.sync_bookmarks` keyed by `X_BOOKMARKS_SYNC_CRON`.
- `app/db/models/core.py` — add `XBookmarkMetadata` model; register in `ALL_MODELS`.
- `app/db/models/core.py` — extend `SourceKind` enum with `X_BOOKMARK`.
- Alembic migration: `x_bookmark_metadata` table + index + GENERATED tsvector column + GIN index. CHECK constraint enum on `x_category`. Source-kind enum extension.
- `app/di/tasks.py` — wire the new task into the Taskiq runtime bundle.
- `app/config/settings.py` — add `X_BOOKMARKS_SYNC_CRON`, `X_BOOKMARKS_DB_PATH` (default `/x_bookmarks/bookmarks.db`).

### Step 3: Classification metadata (Area 4)

Folds into Step 2. The `x_category` column is populated by the ingestor from `bookmarks.db` at write time; no separate classify pass. Unit tests cover the seven-value enum mapping and the CHECK constraint rejecting unknown values.

### Step 4: MCP search tool (Area 2)

- `app/mcp/tools/x_search.py` — tool definition + JSON schema + handler.
- `app/mcp/server.py` — register the tool.
- Tool handler invokes the Postgres FTS query above via `Database.session()`; no subprocess.
- Tests: empty corpus, single match, category filter, ranking ordering, both languages of `plainto_tsquery` (English default; future Russian support deferred).

### Step 5: Wiki indexer (Area 3)

- `app/tasks/x_wiki_sync.py` — Taskiq cron task `ratatoskr.x.sync_wiki` keyed by `X_WIKI_SYNC_CRON`.
- `app/infrastructure/vector/` — extend `VectorIndexedEntityAdapter` registration to cover `entity_type="x_wiki"`; deterministic `point_id = sha256(file_path)`.
- `app/config/settings.py` — add `X_WIKI_SYNC_CRON`, `X_WIKI_LIBRARY_PATH` (default `/x_bookmarks/library`).
- Orphan deletion path: compute path set difference, hard-delete Qdrant points.

### Step 6: `/x_possible` Telegram command (Area 5)

- `app/adapters/telegram/command_handlers/x_possible.py` — handler implementing the flow above.
- `app/adapters/telegram/command_registry.py` — register the command (`adding-telegram-command` skill).
- `app/prompts/en/x_possible_help.txt` + `app/prompts/ru/x_possible_help.txt` — mirrored help strings.
- `app/config/settings.py` — add `X_IDEAS_PATH` (default `/x_bookmarks/ideas`).
- Fallback path: if no `ideas/*.json` exists, reply with the friendly EN/RU message.

### Step 7: Skill registration + documentation

- Host: run `ft skill install`; verify with `ft skill show`.
- `ratatoskr-repositories/CLAUDE.md` (workspace) — note that the `/fieldtheory` skill is installed; note `ft sync` should run before context-sensitive agent sessions.
- `ratatoskr/CLAUDE.md` — note the new Taskiq tasks (`sync_bookmarks`, `sync_wiki`) and the env-var additions.
- `ratatoskr/docs/reference/x-bookmarks-host-setup.md` (new) — host-side `ft` installation, `ft auth` flow, launchd/systemd/cron unit samples (`ratatoskr/ops/host-units/`), Pi-mode-A (direct) vs Pi-mode-B (Mac-rsync-to-Pi) walkthroughs.
- `ratatoskr/docs/SPEC.md` — add a "x_bookmarks integration" entry pointing here.

### Step 8: Review pass

- Reviewer agent checks: correlation IDs preserved across the new code paths; URL normalization always via `app/core/url_utils.py`; no ad-hoc `AsyncSession` outside `app/db/session.py`; both EN/RU prompt files updated; no `docker build` use (always `docker compose build`); no hand-edits to generated OpenAPI files.
- Full `make format && make lint && make type` from `ratatoskr/` root.
- Manual smoke: `ft sync` on host, wait 15 minutes, confirm bookmarks appear in Postgres; `x_search` via MCP returns ranked results; `/x_possible` returns the friendly fallback before any `ft possible run`, and idea nodes after.

---

## Deferred to v2

- **`/x_promote <bookmark>`.** Operator-triggered transition from `processing_status=x_imported` to `pending`, hand-off to the URL processor for full summarization. Schema-compatible with v1.
- **`currently_bookmarked` view.** A read-only Postgres view (or a materialized column) that surfaces which bookmarks are still in `ft`'s active set. Useful for "show me what's still on my reading list" queries. Schema-additive; v1 does not depend on it.
- **Host wrapper daemon for on-demand `ft` invocations.** Enables `/x_bookmarks_sync` on-demand from Telegram, and a v2 mode where `/x_possible` triggers `ft possible run` directly. Adds a Unix-socket/HTTP shim on the host; container code adds an `httpx` path; no data-model change.
- **Russian-language FTS.** Default in v1 is `plainto_tsquery('english', :query)`; bookmarks with Russian-language tweet text rank poorly. Deferred until the corpus actually contains enough Russian content to motivate the switch to `pg_trgm` or `simple` text-search config.
- **Mobile API surface for bookmarks.** Bookmark corpus is reachable via MCP and Telegram in v1; a dedicated `/v1/x_bookmarks/bookmarks` endpoint is deferred to when the mobile clients have a concrete use case.

---

## Why this shape

Six locked decisions underpin this design. Each was chosen against concrete alternatives in the requirements pass; the rationale below is condensed from the decision journal (`DEC-001`, `DEC-001b`, `DEC-002`) and scratchpad notes.

1. **Ingestion fidelity = "copy ft's text, skip the summarizer" (DEC-001, confidence 78, journaled).** Single-tenant deployments cannot absorb thousands-of-bookmarks-times-LLM-cost per sync. The summarizer adds no quality over a 280-char tweet body. `ft` already did the heavy extraction; ratatoskr inherits it.
2. **Mirror `tweet_text` into Postgres (DEC-001b, confidence 85, journaled).** Lets the MCP tool live entirely in Postgres-land; collapses three otherwise expensive consistency properties; ~1 MB cost at v1 scale.
3. **Host-side `ft` with read-only mount (DEC-002, confidence 80, journaled).** Keeps the Python image slim; preserves the existing scraper-sidecar pattern; isolates `ft`'s X-auth state on the host where it was captured; makes `/x_possible` an O(1) JSON read.
4. **Bookmarks immortal, wiki ephemeral (Q4, confidence 85).** `ft`'s disappearance signal is noisy for bookmarks but reliable for wiki files. Schema reflects this: no lifecycle columns on the join table; orphan deletion only on the wiki path.
5. **Cadence: 15-minute container delta + hourly wiki walk + hourly host sync + manual host ideas (Q5, confidence 82).** Asymmetric pairing covers both scheduled and ad-hoc host gestures. Env-var-tunable for operator control.
6. **`/x_possible` inherits `ALLOWED_USER_IDS` (Q6, confidence 92).** Single-tenant operating model already provides the right level of restriction. Zero new auth code; consistent with every other command.

The cumulative effect: bookmark ingestion adds one Postgres table, two Taskiq tasks, one MCP tool, one Telegram command, one new source-kind value, and zero subprocess calls to `ft` from the container. Image footprint unchanged; LLM cost zero on the bookmark path; correlation-ID discipline intact; the four ratatoskr operating rules that matter here (URL normalization via `url_utils`, `Database` is the sole DB entry point, async only, EN/RU prompt parity) are preserved without exception.
