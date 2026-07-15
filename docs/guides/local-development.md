# Local Development

This guide sets up the Ratatoskr backend for development. The editable web
frontend is maintained in the sibling `ratatoskr-web` repository; this repository
contains its backend integration and deployment artifact only.

## Prerequisites

- Python 3.13 or 3.14;
- [`uv`](https://docs.astral.sh/uv/);
- Docker with Compose;
- Git.

Node.js is required only when working on `ratatoskr-web` or rebuilding its bundle.

## Bootstrap the backend

```bash
git clone https://github.com/po4yka/ratatoskr.git
cd ratatoskr
make bootstrap
```

`make bootstrap` performs the repository-owned setup:

1. installs all Python extras and development tools with `uv sync`;
2. installs the pre-commit hooks;
3. starts PostgreSQL, Redis, and Qdrant with the base and development Compose
   files;
4. applies Alembic migrations;
5. creates demo data for user `424242`.

Override ports or the demo user when the defaults conflict with another local
stack:

```bash
DEV_USER_ID=123456 \
POSTGRES_HOST_PORT=15432 \
REDIS_HOST_PORT=16379 \
QDRANT_HOST_PORT=16333 \
make bootstrap
```

The default local service endpoints are:

| Service | Endpoint |
| --- | --- |
| PostgreSQL | `127.0.0.1:5432`, database `ratatoskr` |
| Redis | `redis://127.0.0.1:6379/0` |
| Qdrant | `http://127.0.0.1:6333` |
| Mobile API | `http://127.0.0.1:18000` after it is started |

Stop the development dependencies and delete their development volumes with:

```bash
make teardown-dev
```

That command is intentionally destructive for the Compose development volumes.

## Configure a real runtime

Bootstrap is enough for database-backed tests and demo API work. Running the bot
or the complete summarization pipeline also requires runtime credentials.

```bash
cp .env.example .env
```

At minimum, configure Telegram identity, the owner allowlist, and the selected
LLM provider. Runtime defaults and non-secret settings live in
`config/ratatoskr.yaml`; environment variables override them. Do not add secrets
to that tracked YAML file.

See [Environment Variables](../reference/environment-variables.md) and
[Configuration File](../reference/config-file.md) for the current ownership of
each setting.

## Start a process

Activate the project environment through `uv run`; a separate shell activation
is optional.

Start the API against the development services:

```bash
DATABASE_URL='postgresql+asyncpg://ratatoskr_app:ratatoskr-dev-password@127.0.0.1:5432/ratatoskr' \
ALLOWED_USER_IDS=424242 \
uv run uvicorn app.api.main:app --reload
```

The direct Uvicorn process listens on `http://127.0.0.1:8000`. The Compose
`mobile-api` service instead publishes container port 8000 at
`http://127.0.0.1:18000`:

```bash
POSTGRES_PASSWORD=ratatoskr-dev-password \
ALLOWED_USER_IDS=424242 \
docker compose \
  -f ops/docker/docker-compose.yml \
  -f ops/docker/docker-compose.dev.yml \
  up -d mobile-api
```

Start the Telegram bot directly with:

```bash
uv run python bot.py
```

Run a single URL through the same summarize graph without Telegram:

```bash
uv run python -m app.cli.summary --url https://example.com
```

The scraper order and platform-specific bypasses are documented in
[Scraper Chain](../explanation/scraper-chain.md).

## Database changes

`Database` in `app/db/session.py` is the only engine/session entry point. Schema
changes require an Alembic revision under `app/db/alembic/versions/`.

```bash
# Render migration SQL without changing the database.
uv run python -m app.cli.migrate_db

# Apply all pending migrations.
uv run python -m app.cli.migrate_db --apply

# Inspect the current and target revisions.
uv run python -m app.cli.migrate_db --status
```

Use the test helpers in `tests/db_helpers_async.py` for integration tests.

## Web frontend

For interactive frontend development, clone `ratatoskr-web` next to this
repository and follow that repository's own instructions:

```bash
cd ../ratatoskr-web
npm ci
npm run dev
```

For a directly launched FastAPI process, `make stage-web` builds the sibling
repository and copies its `dist/` output into `app/static/web/`. Local staged
assets are not an input to release Docker images. Release images consume the
reviewed archive built from the SHA in `ops/docker/ratatoskr-web.commit`; maintainers
refresh that artifact with `make web-bundle`.

See [Web Frontend Integration](../reference/frontend-web.md) for the ownership and
release boundary.

## Validation

Start narrow, then expand according to the change:

```bash
# Focused test.
uv run pytest tests/path/to/test_file.py -q

# Unit suite.
make test-unit

# Formatting and lint checks.
ruff format --check .
ruff check .

# Application type check.
make type

# Full test suite.
make test
```

API changes additionally require:

```bash
make generate-openapi
make check-openapi-drift
make check-openapi-validate
make check-openapi
```

Generated OpenAPI files are committed, but they must never be edited by hand.

Useful specialized checks include:

```bash
make check-lock
make check-layout
make security-bandit
make static-checks
```

`make format` modifies files. Use the explicit `--check` commands when you only
want validation.

## Common diagnostics

```bash
# API health for a direct Uvicorn process.
curl --fail http://127.0.0.1:8000/health

# API health for the Compose service.
curl --fail http://127.0.0.1:18000/health

# PostgreSQL connectivity.
docker compose \
  -f ops/docker/docker-compose.yml \
  -f ops/docker/docker-compose.dev.yml \
  exec postgres pg_isready -U ratatoskr_app -d ratatoskr

# Current Alembic state using the local database.
DATABASE_URL='postgresql+asyncpg://ratatoskr_app:ratatoskr-dev-password@127.0.0.1:5432/ratatoskr' \
uv run python -m app.cli.migrate_db --status
```

For correlation-ID tracing and subsystem-specific failure modes, use
[Troubleshooting](../reference/troubleshooting.md).
