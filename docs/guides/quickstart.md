# Quickstart: first summary

Run the maintained Docker Compose stack and send one article URL to your Telegram bot.

## Prerequisites

- Docker with Compose v2;
- Telegram BotFather token;
- Telegram API ID/hash from `my.telegram.org`;
- your numeric Telegram user ID;
- an OpenRouter API key for the default LLM path.

## 1. Clone and configure

```bash
git clone https://github.com/po4yka/ratatoskr.git
cd ratatoskr
cp .env.example .env
```

Edit `.env` and set the seven first-run values:

```env
API_ID=
API_HASH=
BOT_TOKEN=
ALLOWED_USER_IDS=
POSTGRES_PASSWORD=
DATABASE_URL=postgresql+asyncpg://ratatoskr_app:<password>@postgres:5432/ratatoskr
OPENROUTER_API_KEY=
```

`config/ratatoskr.yaml` supplies the default non-secret model and runtime choices. If you select a direct OpenAI, Anthropic, or Ollama adapter, replace the OpenRouter secret with the selected provider's settings; see [Configure LLM Provider](configure-llm-provider.md).

## 2. Build, migrate, and start

```bash
docker compose -f ops/docker/docker-compose.yml build
docker compose -f ops/docker/docker-compose.yml up -d postgres redis qdrant
docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db --apply
docker compose -f ops/docker/docker-compose.yml up -d
```

Check the core services:

```bash
docker compose -f ops/docker/docker-compose.yml ps
docker compose -f ops/docker/docker-compose.yml logs --tail=80 ratatoskr worker scheduler mobile-api
curl -fsS http://127.0.0.1:18000/health/ready
```

## 3. Send a URL

Open the bot in Telegram from an ID listed in `ALLOWED_USER_IDS`, press Start, and send a public article URL. Success means the bot returns a formatted structured summary and the request reaches a terminal success state.

The default chain uses in-process providers and does not require cloud Firecrawl. Optional self-hosted sidecars can be enabled later:

```bash
FIRECRAWL_SELF_HOSTED_ENABLED=true \
docker compose -f ops/docker/docker-compose.yml \
  --profile with-scrapers up -d --build
```

## Troubleshooting

- Configuration failure: compare `.env` with `.env.example` and inspect the first service error.
- Schema mismatch: run `python -m app.cli.migrate_db --status` in the application environment, then apply reviewed migrations with `--apply`.
- LLM failure: verify the selected provider key/model and provider account status.
- No bot response: verify the Telegram credentials and that your numeric ID is allowlisted.
- Extraction failure: inspect the Error ID through [Troubleshooting](../reference/troubleshooting.md).

For TLS, backups, monitoring, optional profiles, and Pi deployment, continue with [Deploy to Production](deploy-production.md).
