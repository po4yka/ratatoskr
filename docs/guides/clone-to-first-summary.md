# Clone to First Summary

This is the Phase 2 onboarding script used for the public quickstart target. It is written so an external tester can run it on a clean Docker host and report the elapsed time without needing to infer hidden setup steps.

## Prerequisites

- Docker with Compose v2+
- Telegram `API_ID`, `API_HASH`, and BotFather `BOT_TOKEN`
- Your Telegram numeric user ID
- OpenRouter API key

## Script

```bash
git clone https://github.com/po4yka/ratatoskr.git
cd ratatoskr

cp .env.example .env
$EDITOR .env

docker compose -f ops/docker/docker-compose.yml build
docker compose -f ops/docker/docker-compose.yml up -d postgres redis qdrant

docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db --apply

docker compose -f ops/docker/docker-compose.yml up -d

docker compose -f ops/docker/docker-compose.yml ps
docker compose -f ops/docker/docker-compose.yml logs --tail=80 ratatoskr
```

Send any article URL to the Telegram bot from a user listed in `ALLOWED_USER_IDS`. The success condition is that the bot replies with a structured summary.

## Optional Self-Hosted Firecrawl Path

```bash
FIRECRAWL_SELF_HOSTED_ENABLED=true \
docker compose -f ops/docker/docker-compose.yml --profile with-scrapers up -d --build

docker compose -f ops/docker/docker-compose.yml --profile with-scrapers ps
```

Smoke a known static page through the CLI once the stack is healthy:

```bash
docker compose -f ops/docker/docker-compose.yml exec ratatoskr \
  python -m app.cli.summary --url https://example.com
```

## Recording

Capture with asciinema when validating the public release flow:

```bash
asciinema rec docs/assets/clone-to-first-summary.cast
```

Record the host type, network speed, Docker image cache state, elapsed time, and whether the self-hosted Firecrawl profile was enabled. Do not edit the timing metadata after recording.
