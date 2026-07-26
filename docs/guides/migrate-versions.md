# Migrate Between Versions

Ratatoskr upgrades are application, configuration, and Alembic migrations. No
release class is assumed to be schema-free: read the release notes and inspect
pending migrations for every upgrade.

For the historical `bite-size-reader` rename, first read
[Migrate from bite-size-reader](migrate-from-bite-size-reader.md).

## Before the maintenance window

1. Record the currently deployed Git revision or image digest.
2. Read `CHANGELOG.md`, the target release notes, and new Alembic revisions under
   `app/db/alembic/versions/`.
3. Create and verify a PostgreSQL backup using
   [Back Up and Restore](backup-and-restore.md).
4. Back up `.env`, the active `ratatoskr.yaml`, and durable files below `data/`.
5. Confirm that the previous application artifact and its matching configuration
   remain available for rollback.

Do not rely on a copied container as a database rollback. Application rollback
and data rollback are separate decisions once a migration has changed the
schema.

## Inspect the target version

From a clean target checkout, compare operator-controlled configuration:

```bash
git diff <deployed-revision>..<target-revision> -- \
  .env.example config/ratatoskr.yaml ops/docker/docker-compose.yml
```

Validate the effective Compose model before changing running services:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml config --quiet
```

Build or pull the exact application artifacts required by your deployment. Avoid
floating tags during a controlled upgrade.

## Inspect and apply migrations

The migration CLI is intentionally dry-run by default. In the same environment
and image that will be deployed:

```bash
# Show the current and target Alembic revisions.
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db --status

# Render pending SQL without changing PostgreSQL.
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db
```

Review the SQL and expected locking/space impact. Then stop application writers
for migrations that are not documented as online-safe:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml stop \
  ratatoskr worker scheduler mobile-api mcp-write
```

Apply the reviewed revision set explicitly:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db --apply
```

Application containers run `migrate_db --check` at startup and refuse to run
against a schema that is not at Alembic head. They do not apply migrations for
you.

## Start and verify

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml up -d

POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml ps

curl --fail http://127.0.0.1:18000/health
```

Then verify the behavior affected by the release:

- migration status reports the expected head;
- bot, worker, scheduler, and API logs contain no startup loop;
- a known summary is readable;
- one new request completes and persists its artifacts;
- search works; check vector reconciliation lag if embedding code or namespace
  changed;
- optional profiles and integrations used by this deployment are healthy.

Use correlation IDs for any failed request and follow
[Troubleshooting](../reference/troubleshooting.md).

## Rollback

Stop and choose the rollback type from observed evidence:

- **Application-only rollback:** safe only when the old application is compatible
  with the migrated schema and configuration. Redeploy the recorded artifact;
  do not downgrade Alembic automatically.
- **Full data rollback:** restore the verified pre-upgrade PostgreSQL backup and
  matching durable files, then deploy the old application and configuration.
  This discards writes made after the backup and therefore requires an explicit
  outage/data-loss decision.
- **Forward fix:** often safer when a migration has already completed and new
  writes exist. Apply a new tested migration or application fix.

Use the restore rehearsal in [Back Up and Restore](backup-and-restore.md) rather
than improvising database drop/recreate commands during an incident.

## Upgrade checklist

- [ ] Deployed and target revisions/digests recorded.
- [ ] Release notes, configuration diff, and Alembic revisions reviewed.
- [ ] PostgreSQL and durable-file backups created and verified.
- [ ] Effective Compose configuration validates.
- [ ] Pending migration SQL reviewed.
- [ ] Required writer outage agreed and applied.
- [ ] Migrations applied with `--apply` and status confirmed.
- [ ] Services, health endpoint, old reads, new writes, and search verified.
- [ ] Rollback compatibility or restore path recorded before reopening traffic.
