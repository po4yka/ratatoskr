# Migrate from `bite-size-reader`

This guide covers the remaining operator-visible rename boundaries for installations that still use the former project name. Current Ratatoskr releases use PostgreSQL and Alembic; they do not contain an automatic importer for an old application SQLite database.

## Before changing anything

1. Back up the old database, media/session files, secrets, and monitoring configuration.
2. Record the exact old image tag or commit so rollback remains possible.
3. Review [Migrate Versions](migrate-versions.md) for any intermediate release requirements.
4. Test the migration against a copy before replacing a production instance.

## Rename checklist

### Repository, images, and services

Replace downstream references to `bite-size-reader` with `ratatoskr`, then recreate the Compose stack from the current file:

```bash
git pull --ff-only
docker compose -f ops/docker/docker-compose.yml build
docker compose -f ops/docker/docker-compose.yml up -d
```

Use the service names declared in `ops/docker/docker-compose.yml`; do not rely on historical standalone-container names.

### Configuration

Start from the current `.env.example` and `config/ratatoskr.yaml`, then copy values deliberately:

- update old repository URLs and display titles;
- use the `ratatoskr` Redis prefix where configured;
- remove retired `KARAKEEP_*` variables;
- set `DATABASE_URL` and `POSTGRES_PASSWORD` for PostgreSQL;
- keep secrets in `.env` or the deployment secret manager, not YAML.

There is no committed environment-rewrite helper. Edit and review the configuration directly.

### Database

Apply the current PostgreSQL schema through Alembic:

```bash
python -m app.cli.migrate_db --apply
```

If the old installation still stores application data in SQLite, preserve that file and plan a separate, verified export/import. Ratatoskr does not currently ship `migrate_sqlite_to_postgres` or an automatic filename-rename path. Telethon session files remain separate integration state and may be copied only when compatible with the current session configuration.

### MCP clients

Update client registrations and any legacy namespace/header overrides:

- resource URIs: `bsr://...` → `ratatoskr://...`;
- server registration name: `bite-size-reader` → `ratatoskr`;
- forwarded headers: `X-BSR-*` → the configured `X-Ratatoskr-*` equivalents.

The current surface is enumerated in [MCP Server](../reference/mcp-server.md): 28 tools and 17 resources.

### Webhooks and clients

Update webhook receivers from `X-BSR-Signature` / `X-BSR-Event` to `X-Ratatoskr-Signature` / `X-Ratatoskr-Event` where those historical headers are still pinned. Reissue browser/mobile/CLI sessions after changing client IDs, cookie names, or token storage namespaces; do not copy refresh tokens between incompatible deployments.

### Monitoring

Replace `bsr_*` metric names, old dashboard UIDs, Loki labels, and log paths in operator-owned dashboards and alerts. Old time series may remain in Prometheus until retention removes them, but current services emit the Ratatoskr names declared in the code and bundled dashboards.

## Verify

```bash
docker compose -f ops/docker/docker-compose.yml ps
docker compose -f ops/docker/docker-compose.yml logs --tail=100 ratatoskr mobile-api worker scheduler
python -m app.cli.migrate_db --check
```

Then verify:

- a whitelisted Telegram user can create a summary;
- `/health/ready` succeeds on the API;
- a current MCP client can list tools/resources;
- PostgreSQL contains the expected users and summaries;
- configured Qdrant retrieval works or can be repaired with `python -m app.cli.reconcile_vector_index --repair`.

## Rollback

Stop the new stack, restore the complete pre-migration backup, and run the recorded old image/config together. Do not point an older binary at a schema that newer Alembic revisions have changed unless that release explicitly documents downgrade compatibility.
