# Changelog

All notable changes to Ratatoskr will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking — Project renamed to Ratatoskr

The project has been renamed from `bite-size-reader` to `ratatoskr`. The rename touches Docker image / container names, the default DB filename, the MCP protocol surface (`bsr://` URIs and `X-BSR-*` headers), the CLI package and config directory, the Carbon web storage keys and refresh cookie, all `bsr_*` Prometheus metric names, and the Loki / Promtail labels. The Karakeep integration is retired in the same release.

**For the full breaking-change inventory and the operator checklist, see [docs/guides/migrate-from-bite-size-reader.md](docs/guides/migrate-from-bite-size-reader.md).** The migration page is the canonical source — historical-record discipline keeps this entry short so the breaking-change list does not drift from the operational guide.

### Breaking — Telegram runtime migrated to Telethon

Ratatoskr now uses Telethon for both the BotFather-token bot adapter and the channel-digest userbot session. `pyrotgfork`/Pyrogram and `pytgcrypto` are no longer runtime dependencies. Existing digest userbot sessions must be recreated with `/init_session` or `python -m app.cli.init_userbot_session`; the migration flow keeps the previous `.session` file untouched until a new Telethon session authenticates successfully, then stores the old file as `<DIGEST_SESSION_NAME>.legacy.bak.session`.

### Added
- Russian-language transcription via [GigaAM-v3 e2e RNN-T](https://huggingface.co/ai-sage/GigaAM-v3) (MIT-licensed, ~230 MB INT8, ~8.4% WER) using the sherpa-onnx export at [`Smirnov75/GigaAM-v3-sherpa-onnx`](https://huggingface.co/Smirnov75/GigaAM-v3-sherpa-onnx). Set `TRANSCRIPTION_LANGUAGE=ru` to switch from the default Kroko English Zipformer; the engine, tokens-mode (char-level Cyrillic), and model bundle are picked automatically. New optional escape hatches `TRANSCRIPTION_BACKEND` and `TRANSCRIPTION_TOKENS_MODE` for custom-model setups. Note: GigaAM-v3 is offline-only (no Russian streaming transducer exists upstream as of 2026-05); the Telegram-reply UX is unchanged but partial-caption streaming would not work on the Russian path. The downloader normalizes GigaAM's `gigaam_v3_e2e_rnnt_*` filenames to the canonical sherpa-onnx layout on disk.
- CPU-only transcription subsystem (sherpa-onnx + ffmpeg, ported and adapted from `yapsnap`). Off by default; opt-in via `TRANSCRIPTION_ENABLED=true` plus the new `[transcription]` extra (`pip install 'ratatoskr[transcription]'`). Three trigger surfaces, all sharing one in-process `TranscriptionService` instance so the ~80 MB Kroko streaming Zipformer model loads exactly once:
  - **`/transcribe` Telegram command** — `/transcribe <url>` fetches via in-process `yt_dlp`; `/transcribe` as a reply to a voice / audio / video_note / video message downloads via Telethon. Transcripts over ~4000 chars are uploaded as a `.txt` attachment.
  - **Voice / audio / video_note auto-handler** — when a previously-unhandled voice or audio message arrives, the bot replies with its transcript. Gated by `TRANSCRIPTION_AUTO_VOICE=true` (default `true` when transcription is enabled). Not persisted as a Summary in v1; durable archiving is a planned follow-up.
  - **YouTube pipeline auto-fill** — when both `youtube-transcript-api` and VTT subtitles return empty, the downloaded video is transcribed locally and the result populates `VideoSourceRequest.audio_transcript_text`. Opt-in via `TRANSCRIPTION_AUTO_URL_PIPELINE=true` (default `false`). Failures are logged and downgraded so the existing "no transcript available" error still fires when every path is exhausted.
  - **Optional diarization** (`TRANSCRIPTION_DIARIZATION_ENABLED=true`) adds `SPEAKER_xx` labels via pyannote-3.0 segmentation (CC-BY-4.0 default) or reverb-v1 (`TRANSCRIPTION_DIARIZATION_MODEL=reverb`; non-commercial license — license notice logged on first download).
  - Full reference: [`docs/explanation/transcription.md`](docs/explanation/transcription.md) and the new [Transcription section](docs/reference/environment-variables.md#transcription-cpu-only-asr) in the env-vars guide.
- Vector-index sync subsystem keeping Qdrant converged with the Postgres `summaries` table: two-writer design — synchronous fast path in the `persist` graph node (read-your-writes freshness) and the Taskiq reconciler `ratatoskr.vector.reconcile` (default cron `*/30 * * * *`) that re-embeds summaries whose `summary_embeddings.last_indexed_at` lags `summaries.updated_at`. Runs in the worker process; configurable via `VECTOR_RECONCILE_ENABLED` / `VECTOR_RECONCILE_CRON` / `VECTOR_RECONCILE_BATCH_SIZE`. - `summary_embeddings` now stamps `content_hash` (SHA256 of the prepared text), `last_indexed_at`, and `index_status` on every write, so re-runs short-circuit when the input text is unchanged.
- Channel digest subsystem with userbot, scheduler, and commands (`/digest`, `/channels`, `/subscribe`, `/unsubscribe`)
- Bot-mediated userbot session initialization via `/init_session` with Telegram Mini App OTP/2FA flow
- gRPC service implementation with comprehensive Python client library and integration tests
- Quality assessment and web verification in summary output
- Critical analysis and caveats sections in summaries
- Embedded image analysis support in PDFs and web articles
- PDF metadata extraction, table of contents parsing, and improved layout handling
- Language filtering in SearchFilters
- Progress tracking for PDF processing and batch operations
- Editable progress messages for LLM and YouTube processing in Telegram
- Typing indicators for long-running operations in Telegram bot
- Full logging and dynamic status updates for batch processing
- Redirect-aware X article link resolver with structured reason codes (`path_match`, `redirect_match`, `canonical_match`, `not_article`, `resolve_failed`)
- Optional manual live smoke script for X article links (`scripts/twitter_article_live_smoke.py`) with per-link JSON diagnostics
- Real-time streaming summary progress: `GET /v1/requests/{id}/stream` SSE endpoint emits `phase` / `section` / `done` / `error` events sourced from a process-wide `StreamHub`. The Telegram URL flow now streams section snapshots via the existing draft coordinator; the web SubmitPage consumes the SSE stream via `useRequestStream` (with polling fallback after two consecutive fatal closes). Gated by `URL_FLOW_STREAMING_ENABLED` (default `true`)

### Removed
- `with-firecrawl` Docker Compose profile alias; use `with-scrapers` instead. Operators with `--profile with-firecrawl` in scripts must update to `--profile with-scrapers`.
- `pyrotgfork`/Pyrogram and `pytgcrypto` runtime dependencies; Telethon is now the only Telegram client stack.
- `nlp` optional extra group and spaCy trained model dependencies (en_core_web_sm, ru_core_news_sm) -- codebase only uses `spacy.blank()` + sentencizer
- `lock-piptools` Makefile target -- `lock-uv` is the canonical dependency locking path
- `PROMPT.md` -- referenced non-existent migration docs
- `app/grpc/` module, `app/protos/`, and `grpc` optional extra -- aspirational gRPC layer never wired into production
- Duplicate versioned migration modules under `app/db/migrations/`; `app/cli/migrations/` is now the sole canonical migration directory used by runtime startup.

### Security
- Update pyjwt 2.11.0 to 2.12.1 (CVE-2026-32597)

### Changed
- Add Docker Compose profiles for self-hosted scrapers, remote cloud Ollama, monitoring, and MCP; default compose config now works without a local `.env` file.
- Publish GHCR `:stable` on non-prerelease semver tags and keep `:latest` disabled.
- Reduce `.env.example` to the five first-run Telegram/OpenRouter values and move optional power-user settings to `ratatoskr.yaml`.
- Add optional `RATATOSKR_CONFIG` / `ratatoskr.yaml` loading with precedence below `.env` and process environment.
- Add OpenAI-compatible cloud Ollama configuration (`LLM_PROVIDER=ollama`) while keeping OpenRouter as the default provider.
- Reject deprecated migration shadow-mode environment variables at startup instead of silently accepting them.
- Rename the active web client ID from `web-carbon-v1` to `web-v1`; existing web/browser sessions may need to sign in again.
- Rename Prometheus alert rule names from the historical `BSR*` prefix to `Ratatoskr*`.
- Clarify that current Docker Compose points at an externally managed self-hosted Firecrawl API via `firecrawl-api:host-gateway`; the in-compose Firecrawl profile is planned separately.
- Add `python -m app.cli.migrate_db --status [/path/to/db.sqlite]` for canonical migration status reporting.
- Replace `uv pip compile --extra dev` with `uv export --only-group dev` across CI workflows, Makefile, and scripts (PEP 735 dependency groups)
- Add retry wrapper around `uv lock --check` in CI to handle transient GitHub CDN failures
- Prune stale paths from coverage_includes.txt and file_size_baseline.json
- Renamed ContentExtractor methods (breaking change for tests)
- Improved PDF extraction flow with async processing and enhanced Russian language detection
- Enhanced "Analyzing with AI" messages with additional context
- Made input validation less strict for better usability
- Hardened X article extraction flow with strict article-path matching plus redirect/canonical resolution before routing
- Added article-stage metadata fields for observability (`article_resolution_reason`, `article_resolved_url`, `article_canonical_url`, `article_id`, `article_extraction_stage`)
- Added Twitter article config flags (`TWITTER_ARTICLE_REDIRECT_RESOLUTION_ENABLED`, `TWITTER_ARTICLE_RESOLUTION_TIMEOUT_SEC`, `TWITTER_ARTICLE_LIVE_SMOKE_ENABLED`)
- Rust interface routing now treats query-suffixed public endpoints (`/health?*`, `/metrics?*`, `/docs?*`, `/openapi.json?*`) as handled routes.
- Rust summary aggregation now trims whitespace-padded numeric strings before parsing (`" 3 "` -> `3`).
- Rust logging bootstrap now falls back to `info` instead of panicking on invalid log-level config.
- Telegram orchestration flow now delegates lifecycle, callback action execution, and URL policy/state to focused collaborators (`TelegramLifecycleManager`, `CallbackActionRegistry` + `CallbackActionService`, `URLBatchPolicyService`, `URLAwaitingStateStore`).
- Mobile API digest/system routers now delegate orchestration to dedicated services (`DigestFacade`, `SystemMaintenanceService`) instead of in-router DB/Redis/file workflows.
- Formatter component boundaries now enforce protocol interfaces at constructor/public seams rather than concrete `*Impl` coupling.
- Project docs refreshed to reflect current architecture boundaries and service decomposition across Telegram/API/formatting flows.
- Project documentation refreshed for dual frontend setup, including a new Carbon web frontend guide (`FRONTEND.md`) and updated deployment/local-dev/quickstart/spec/API docs.
- Project documentation refreshed for mixed-source aggregation coverage, rollout flags, bundle observability, and FastAPI aggregation endpoints.

### Fixed
- Updated tests for renamed ContentExtractor methods
- Fixed PDF processing async extraction and Russian detection issues
- Fixed batch processing stalls with improved UX and logging
- Limited verification scope to prevent performance issues
- Implemented integrity checks for data validation
- Improved Playwright X article scraping reliability by moving to locator-first readiness and selector fallback diagnostics
- Fixed Unicode boundary corruption in Rust `questions_answered` parsing for `Question:/Answer:` textual payloads.
- Fixed Rust entity normalization to avoid emitting metadata-only values (for example `type`, `confidence`) as entity names.
- Fixed protocol seam drift in response formatting stack by aligning protocol contracts with actual consumer method signatures (message-thread safe replies, admin logging, draft controls, text/link helpers, and summary forwarding signatures).

## Release History

_This project is currently in active development. Formal versioned releases will be documented here._

---

## How to Contribute to This Changelog

When submitting a pull request:

1. Add your changes under the `[Unreleased]` section
2. Use one of these categories: `Added`, `Changed`, `Deprecated`, `Removed`, `Fixed`, `Security`
3. Write in the imperative mood ("Add feature" not "Added feature")
4. Link to relevant issues or PRs where applicable
5. Credit contributors with `@username` or full name

### Category Guidelines

- **Added** for new features
- **Changed** for changes in existing functionality
- **Deprecated** for soon-to-be removed features
- **Removed** for now removed features
- **Fixed** for any bug fixes
- **Security** for vulnerability fixes

## Versioning Strategy

This project follows [Semantic Versioning](https://semver.org/):

- **MAJOR** version for incompatible API changes
- **MINOR** version for backwards-compatible functionality additions
- **PATCH** version for backwards-compatible bug fixes

---

**Maintainers:** When cutting a release, move unreleased changes to a new version section with:
- Version number and release date: `## [1.0.0] - 2026-02-09`
- GitHub compare link at bottom: `[1.0.0]: https://github.com/po4yka/bite-size-reader/compare/v0.9.0...v1.0.0`
- Contributor acknowledgments
