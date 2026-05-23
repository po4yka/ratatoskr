# Ratatoskr

A self-hosted Telegram bot that turns the things you read, watch, and forward into a searchable, structured archive — web articles, YouTube videos, Twitter / X posts, forwarded channel messages, or any mix of those bundled together. Owner-only by design, runs as a single Docker container, stores everything in PostgreSQL.

[![CI](https://github.com/po4yka/ratatoskr/actions/workflows/ci.yml/badge.svg)](https://github.com/po4yka/ratatoskr/actions/workflows/ci.yml) [![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/downloads/) [![Docker](https://img.shields.io/badge/docker-ready-blue?logo=docker)](ops/docker/Dockerfile) [![License](https://img.shields.io/badge/license-see%20LICENSE-lightgrey)](LICENSE)

---

## Why Ratatoskr?

- **Self-hosted, single-tenant.** Your data, your server, your Telegram-API quota. The bot only answers IDs in `ALLOWED_USER_IDS`.
- **Pluggable cost.** Bring your own OpenRouter key (or OpenAI / Anthropic). Free DeepSeek / Gemini Flash models cover most workloads out of the box; paid models are an opt-in upgrade.
- **Built for triage, not bookmarking.** Each summary is a strict 35+ field JSON contract — TLDR, key ideas, entities, key stats, topics, reading time — bound through an explicit default contract descriptor so prompts, schemas, and validation stay in sync as providers evolve.
- **Multi-source aggregation.** Bundle a YouTube clip with two web articles and a forwarded post, get one synthesized output with per-source provenance.

## 30-second install

```sh
git clone https://github.com/po4yka/ratatoskr.git
cd ratatoskr
cp .env.example .env                  # set the 7 required values
docker compose -f ops/docker/docker-compose.yml up -d
```

Required env vars (everything else has sensible defaults):

```env
API_ID=                # https://my.telegram.org/apps
API_HASH=
BOT_TOKEN=             # @BotFather
ALLOWED_USER_IDS=      # your Telegram user ID
POSTGRES_PASSWORD=     # password for the ratatoskr_app DB role
DATABASE_URL=          # postgresql+asyncpg://ratatoskr_app:${POSTGRES_PASSWORD}@postgres:5432/ratatoskr
OPENROUTER_API_KEY=    # https://openrouter.ai
```

Optional scraper, YouTube, Twitter/X, MCP, and provider tuning now live in `ratatoskr.yaml`; see [Optional YAML Configuration](docs/reference/config-file.md). `JWT_SECRET_KEY` is required only when enabling web/API/browser-extension JWT auth.

Compose profiles:

- `with-scrapers` starts the full self-hosted scraper sidecar stack: `firecrawl-api` (port 3002), `crawl4ai` (port 11235), and `defuddle-api` (port 3003) plus their dependencies. Cloud Firecrawl is not used for article extraction; there is no `FIRECRAWL_API_KEY` requirement. Set `FIRECRAWL_SELF_HOSTED_ENABLED=true` to activate the Firecrawl rung in the scraper chain.
- `with-cloud-ollama` adds a remote OpenAI-compatible Ollama reachability check; set `LLM_PROVIDER=ollama` and `OLLAMA_*` values to use it. It does not start a local model server.
- `with-monitoring` starts Prometheus, Grafana, Loki, Promtail, node-exporter, and OpenTelemetry / Tempo.
- `mcp`, `mcp-write`, and `mcp-public` start the optional MCP server variants.

For the guided walkthrough, see the [5-minute Quickstart Tutorial](docs/guides/quickstart.md). For the full setup including TLS, monitoring, and backups, see [Deploy to Production](docs/guides/deploy-production.md). The onboarding script is tracked in [Clone to First Summary](docs/guides/clone-to-first-summary.md) for repeatable 10-minute validation runs.

## What it does

**Web articles.** A multi-provider scraper chain — Scrapling → direct PDF → Crawl4AI → Firecrawl (self-hosted only) → Defuddle → Playwright → Crawlee → direct HTML → ScrapeGraphAI — extracts clean content, then OpenRouter generates a summary against the strict JSON contract. The default order is overridable via `SCRAPER_PROVIDER_ORDER`. Cloud Firecrawl is not used; all sidecars (Firecrawl, Crawl4AI, Defuddle) run via the `with-scrapers` Docker Compose profile. JS-heavy hosts can be configured to skip straight to a browser-based provider.

**YouTube videos.** Detects every common URL form (watch, shorts, live, embed, music, mobile). Pulls transcripts via `youtube-transcript-api` (manual subtitles preferred), downloads the video at 1080p with `yt-dlp` for archival, then summarizes from the transcript. Storage is capped per-video and in total, with optional auto-cleanup. See [Configure YouTube Download](docs/guides/configure-youtube-download.md).

**Twitter / X.** Two-tier extraction: Firecrawl public scraping by default; opt-in authenticated Playwright with your own `cookies.txt` when you need protected accounts, deep threads, or X Articles. GraphQL interception for tweets / threads, DOM scraping for X Articles, redirect-aware article URL resolver. See [Configure Twitter / X Extraction](docs/guides/configure-twitter-extraction.md).

**Forwarded posts and bundles.** Forward a Telegram channel post and get the same structured summary; or use `/aggregate` to bundle one or more URLs (plus optional forwards / attachments) into a single provenance-tracked synthesis. Channel-digest scheduling on top of all this turns subscribed channels into a periodic recap.

## What else it includes

- **Web frontend** — A React + TypeScript UI at `/web/*`, served by FastAPI. Hybrid auth: Telegram WebApp, Telegram Login Widget, or nickname/email + password with Remember Me. Set `CREDENTIALS_LOGIN_PEPPER` (≥32 chars, generated separately from `JWT_SECRET_KEY`) and bootstrap the password once via `ratatoskr credentials set --user-id <your_telegram_id> --nickname <name>`. See [Web Frontend](docs/reference/frontend-web.md) · [ratatoskr-web repo](https://github.com/po4yka/ratatoskr-web).
- **Mobile REST API** — JWT-authenticated REST API with device sync, collections, and aggregations. See [Mobile API Reference](docs/reference/mobile-api.md).
- **Real-time progress streaming** — `GET /v1/requests/{id}/stream` is a Server-Sent Events stream of phase + section events for in-flight summaries. Consumed by the web SubmitPage and by the Telegram URL flow's progressive draft-message updates.
- **MCP server** — Expose summaries and search to external AI agents via the Model Context Protocol. See [MCP Server](docs/reference/mcp-server.md).
- **Multi-agent pipeline** — ContentExtraction, Summarization, Validation, and WebSearch agents coordinate via the classic orchestrator; LangGraph backs the summarize/validate retry graph and LangChain structured output is used where models support it. See [Multi-Agent Architecture](docs/explanation/multi-agent-architecture.md).
- **Semantic search** — Qdrant vector store with local (sentence-transformers) or Gemini embedding providers. Summary and repository vectors are reconciled through deterministic point IDs, fast-path writes, and optional CocoIndex live flows.
- **GitHub repositories** — Index your starred GitHub repos as a searchable knowledge base. Paste a `github.com/<owner>/<repo>` URL for immediate ingestion, or connect a PAT / OAuth Device Flow token and let the daily sync import and LLM-analyze your entire stars list automatically. Repo analysis prefers LangChain structured output and analyzed repos are exported to Qdrant by the repository fast path plus CocoIndex. See [Setup: GitHub integration](#setup-github-integration-optional) below.
- **Channel digests** — Subscribe to Telegram channels and receive periodic structured recaps.
- **RSS feeds** — Ingest RSS feed items as summarization sources.
- **Text-to-speech** — Optional ElevenLabs TTS audio generation for summaries.

## Setup: GitHub integration (optional)

Ratatoskr can index your GitHub repositories as a first-class searchable archive. All three steps below require a running Qdrant instance (`QDRANT_URL`). To use the CocoIndex reconciler for repository and summary vectors, install the extra with `pip install -e ".[cocoindex]"` and set `RATATOSKR_COCOINDEX_ENABLED=1`.

**1. Generate a Fernet encryption key** (required for token storage):

```bash
python tools/scripts/generate_github_encryption_key.py
```

Copy the output into your `.env`:

```env
GITHUB_TOKEN_ENCRYPTION_KEY=<paste key here>
```

**2. Connect a GitHub token** — choose one method:

- **Fine-grained PAT (recommended, no extra config):** Create a token at [github.com/settings/personal-access-tokens](https://github.com/settings/personal-access-tokens/new) with scopes `read:user` and `public_repo`. Submit it via the web UI's GitHub Integration settings panel (`POST /v1/auth/github/pat`).

- **OAuth Device Flow (optional, requires an OAuth App):** Register a GitHub OAuth App at [github.com/settings/applications/new](https://github.com/settings/applications/new). Use any name (e.g., "Ratatoskr") and any homepage URL; no callback URL is needed. Copy the client ID and secret into `.env`:

  ```env
  GITHUB_OAUTH_APP_CLIENT_ID=<client_id>
  GITHUB_OAUTH_APP_CLIENT_SECRET=<client_secret>
  ```

  Then use the Device Flow button in the GitHub Integration settings panel. A short code and `github.com/login/device` URL are displayed; enter the code in your browser to authorize. Device Flow requires a running Redis instance (`REDIS_URL`).

**3. Sync runs automatically** at 02:00 UTC daily (`GITHUB_SYNC_CRON`). To trigger a manual sync:

```bash
python -m app.cli.sync_github_stars --user-id <your_telegram_user_id>
```

A budget cap (`GITHUB_LLM_DAILY_BUDGET=100`) limits LLM analysis calls per run; repos beyond the cap show as "still indexing" in the web UI and are re-queued the next day. See [GitHub Repository Ingestion](docs/explanation/github-repository-ingestion.md) for architecture details and all configuration options.

## Configure & extend

| What | Where |
| --- | --- |
| First-run env and optional YAML config | [docs/reference/environment-variables.md](docs/reference/environment-variables.md) · [docs/reference/config-file.md](docs/reference/config-file.md) |
| Production deploy, monitoring, backups, TLS | [docs/guides/deploy-production.md](docs/guides/deploy-production.md) |
| Architecture diagram, request lifecycle, subsystem index | [docs/explanation/architecture-overview.md](docs/explanation/architecture-overview.md) |
| Mobile REST API (JWT auth, sync, aggregations) | [docs/reference/mobile-api.md](docs/reference/mobile-api.md) |
| Web frontend (`/web/*`) | [docs/reference/frontend-web.md](docs/reference/frontend-web.md) · [ratatoskr-web](https://github.com/po4yka/ratatoskr-web) |
| MCP server for external AI agents | [docs/reference/mcp-server.md](docs/reference/mcp-server.md) |
| FAQ / troubleshooting | [docs/explanation/faq.md](docs/explanation/faq.md) · [docs/reference/troubleshooting.md](docs/reference/troubleshooting.md) |
| Full doc index | [docs/README.md](docs/README.md) |

## Where to next

| If you want to … | Start here |
| --- | --- |
| **Use it.** Run the bot, try out features, configure a knob. | [Quickstart Tutorial](docs/guides/quickstart.md) → [How-to guides](docs/README.md) |
| **Deploy and operate it.** Production install, monitoring, backups, upgrades. | [Deploy to Production](docs/guides/deploy-production.md) → [Backup and Restore](docs/guides/backup-and-restore.md) → [Optimize Performance](docs/guides/optimize-performance.md) |
| **Extend it.** Read the code, write a feature, integrate a client. | [CLAUDE.md](CLAUDE.md) (codebase tour) → [docs/SPEC.md](docs/SPEC.md) (canonical contract) → [Local Development Tutorial](docs/guides/local-development.md) |

---

Released under the terms of [LICENSE](LICENSE). Bug reports, feature requests, and pull requests are welcome at [github.com/po4yka/ratatoskr/issues](https://github.com/po4yka/ratatoskr/issues).

Built on the shoulders of [Telethon](https://github.com/LonamiWebs/Telethon), [Scrapling](https://github.com/D4Vinci/Scrapling), [Firecrawl](https://github.com/mendableai/firecrawl) (self-hosted), [Crawl4AI](https://github.com/unclecode/crawl4ai), [Defuddle](https://github.com/kepano/defuddle) (self-hosted Node sidecar), [Playwright](https://playwright.dev/), [Crawlee](https://crawlee.dev/), [ScrapeGraphAI](https://github.com/ScrapeGraphAI/Scrapegraph-ai), [Qdrant](https://github.com/qdrant/qdrant), [Pydantic](https://docs.pydantic.dev/), [OpenRouter](https://openrouter.ai/), [FastAPI](https://fastapi.tiangolo.com/), and [yt-dlp](https://github.com/yt-dlp/yt-dlp).
