# Configuration File

`ratatoskr.yaml` owns non-secret operational settings. `.env` owns secrets,
credentials, PII, and deployment connection strings. The maintained templates
are:

- `config/ratatoskr.yaml` — configuration baked into the repository/container;
- `config/ratatoskr.yaml.example` — operator-oriented example;
- `.env.example` — first-run secret/deployment template.

Do not copy model names or complete setting catalogs into other documents. The
Pydantic models in `app/config/` remain the executable source of types, defaults,
required fields, and validators.

## Search order

The loader reads the first existing path:

1. `RATATOSKR_CONFIG`, when set;
2. `./ratatoskr.yaml`;
3. `./config/ratatoskr.yaml`;
4. `/app/config/ratatoskr.yaml`.

An explicit `RATATOSKR_CONFIG` path does not fall through to the remaining
locations when the file is missing.

## Precedence

```text
non-secret YAML  >  process environment  >  .env / constructor input  >  field default
secret environment                         >  field default
```

Fields marked with `SECRET_MARKER` in `app/config/_secret_marker.py` are removed
from YAML input and logged as `yaml_secret_keys_ignored`. Place API keys, tokens,
database/JWT secrets, encryption keys, OAuth secrets, and user allowlists in a
secret environment source.

## YAML mapping

Top-level YAML names match `Settings` attributes. Nested names match Pydantic
field names, while uppercase environment aliases come from each field's
`validation_alias`.

```yaml
runtime:
  log_level: INFO
  preferred_lang: auto

scraper:
  profile: balanced
  browser_enabled: true
  provider_order:
    - reddit
    - hn
    - scrapling
    - direct_pdf
    - crawl4ai
    - firecrawl
    - defuddle
    - cloakbrowser
    - playwright
    - crawlee
    - direct_html
    - scrapegraph_ai
    - webwright

youtube:
  enabled: true
  storage_path: /data/videos
```

Lists may be expressed as YAML lists. Dictionary-valued fields remain native
YAML mappings. Scalar values are passed through the same field validators used
for environment overrides.

Model and attachment settings that have no code default must remain present in
YAML or be supplied by environment. Removing the file is therefore not a safe
way to “reset to defaults” for every deployment.

## Common sections

| Section | Owning model |
| --- | --- |
| `runtime` | `RuntimeConfig` |
| `openrouter`, `openai`, `anthropic`, `ollama`, `llm_budget` | `app/config/llm.py` |
| `telegram`, `telegram_limits`, `batch_processing` | `app/config/telegram.py` |
| `database` | `DatabaseConfig` |
| `redis` | `RedisConfig` |
| `api_limits`, `auth`, `sync` | `app/config/api.py` |
| `scraper` | `ScraperConfig` |
| `firecrawl` | `FirecrawlConfig` |
| `youtube`, `attachment` | `app/config/media.py` |
| `transcription` | `TranscriptionConfig` |
| `web_search`, `mcp`, `batch_analysis`, `embedding`, `qdrant` | `app/config/integrations.py` |
| `twitter`, `social`, `signal_ingestion`, `rss` | matching files in `app/config/` |
| `digest`, `email`, `elevenlabs`, `push` | matching files in `app/config/` |
| `github`, `git_backup`, `x_bookmarks`, `ai_backup` | matching files in `app/config/` |
| `retention`, `backup`, `import_export` | matching files in `app/config/` |
| `otel`, `sentry`, `langgraph_checkpoint` | matching files in `app/config/` |

See [Environment Variables](environment-variables.md) for secret/deployment
inputs and ownership of less common sections.

## Per-role differences

Compose may override the same field differently for bot, worker, API, scheduler,
or MCP roles. Examples include Redis requirements, process-role telemetry, and
worker capacity. Keep those differences in `ops/docker/docker-compose.yml` or an
explicit override file rather than broadcasting one YAML value to every role.

Inspect the final Compose environment with:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml config
```

## Runtime updates

`/setmodel` updates supported model keys through the config loader's
`SECTION_MAP`. `ConfigReloader` watches the same active YAML path for model
changes. This is not a general-purpose hot-reload mechanism: restart affected
roles after other configuration changes unless the owning subsystem explicitly
documents reload behavior.

## Validate

Use the same environment and working directory as the target process:

```bash
uv run python -c \
  'from app.config import load_config; load_config(); print("configuration valid")'
```

Validation only proves that settings parse. Follow it with connectivity checks
for PostgreSQL, Redis, Qdrant, enabled sidecars, and the selected LLM provider.

When startup reports a missing or invalid value, fix the owning source instead
of weakening its validator. Deprecated scraper and migration-shadow environment
names are rejected with migration guidance.
