# Back Up and Restore

PostgreSQL is Ratatoskr's authoritative datastore. A recoverable instance backup
also preserves operator configuration and any durable files that cannot or should
not be fetched again. Qdrant and several media caches are derived and can be
rebuilt when their snapshots are unavailable.

Run the examples from the repository root against
`ops/docker/docker-compose.yml`.

## What is durable

| Data | Default location | Recovery role |
| --- | --- | --- |
| PostgreSQL | Compose volume `ratatoskr_postgres_data` | Authoritative; always back up. |
| Application files | Host `data/`, mounted at `/data` | Sessions, exports, media, and other runtime files. |
| Git mirrors | Compose volume mounted at `/data/git-mirrors` | Full cloned history; preserve when remote recovery is insufficient. |
| Qdrant | Compose volume `qdrant_data` | Derived index; snapshot or rebuild from PostgreSQL. |
| Redis | Compose volume with persistence disabled by the default command | Ephemeral cache/locks/rate state; not a recovery source. |
| Configuration | `.env` and active `ratatoskr.yaml` | Required to reproduce a deployment; contains secrets. |

Per-user `/v1/backups` and Telegram `/backup` exports are portability artifacts,
not full instance backups. They omit operational tables, configuration, and some
runtime files.

## Automated PostgreSQL backups

The `pg-backup` Compose service runs `pg_dump --format=custom` on `BACKUP_CRON`
(`0 3 * * *` UTC by default). It writes to the host directory selected by
`BACKUP_HOST_DIR` (`data/postgres-backups` by default) and deletes local files
older than `BACKUP_RETENTION_DAYS`.

On Raspberry Pi production, `make pi-deploy-all` builds this sidecar locally for
Linux/ARM64, streams the exact Compose image to the Pi, and recreates it without
building remotely. The Pi overlay defaults `BACKUP_RUN_ON_START=true`; the
container becomes healthy only after that initial dump has created its artifact
and `ratatoskr_pg_backup.prom` metric. Use
`make pi-deploy SERVICE=pg-backup` to deploy only the sidecar.

Start the sidecar and run a backup immediately:

```bash
POSTGRES_PASSWORD=... BACKUP_ENCRYPTION_KEY=... \
docker compose -f ops/docker/docker-compose.yml up -d postgres pg-backup

POSTGRES_PASSWORD=... BACKUP_ENCRYPTION_KEY=... \
docker compose -f ops/docker/docker-compose.yml exec -T pg-backup \
  ratatoskr-pg-backup-run
```

Every successful run writes a dump plus JSON metadata containing size and
SHA-256. `BACKUP_ENCRYPTION_KEY` encrypts sidecar dumps with the sidecar's OpenSSL
format. `BACKUP_S3_*` settings optionally copy the artifacts to object storage.
Keep the encryption key and object-store credentials outside the backed-up host.
Encryption is required by default: a missing key or invalid
`BACKUP_REQUIRE_ENCRYPTION` value makes the run fail before `pg_dump` starts,
and off-host copies are never allowed without encryption. For isolated local
development only, `BACKUP_REQUIRE_ENCRYPTION=false` explicitly permits a
mode-`0600` plaintext artifact; do not use that override in production.

Verify the default encrypted dump by decrypting it only into `pg_restore`:

```bash
BACKUP_ENCRYPTION_KEY=... \
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_ENCRYPTION_KEY \
  -in data/postgres-backups/ratatoskr-postgres-YYYYMMDDTHHMMSSZ.dump.enc \
| docker run --rm -i --entrypoint pg_restore postgres:17 --list >/dev/null
```

For an explicit local-development plaintext dump, list it directly:

```bash
docker run --rm -i --entrypoint pg_restore postgres:17 --list \
  < data/postgres-backups/ratatoskr-postgres-YYYYMMDDTHHMMSSZ.dump \
  >/dev/null
```

Also test retrieval and decryption of off-host copies. A successful upload log is
not a restore rehearsal.

## Manual full backup

Choose one timestamp and restricted destination:

```bash
BACKUP_TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="backups/$BACKUP_TS"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
```

### PostgreSQL

`pg_dump` provides a transactionally consistent database snapshot while the
server is live:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml exec -T postgres \
  pg_dump --format=custom --no-owner --no-privileges \
  -U ratatoskr_app -d ratatoskr \
  > "$BACKUP_DIR/ratatoskr.dump"

docker run --rm -i --entrypoint pg_restore postgres:17 --list \
  < "$BACKUP_DIR/ratatoskr.dump" >/dev/null
```

The second command must exit successfully and the dump must be non-empty.

### Application files and configuration

For a point-in-time copy consistent with PostgreSQL, pause writers before copying
files that may change:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml stop \
  ratatoskr worker scheduler mobile-api mcp-write

tar -C . -czf "$BACKUP_DIR/data.tar.gz" data
tar -C . -czf "$BACKUP_DIR/config.tar.gz" \
  .env config/ratatoskr.yaml
```

If the active YAML is selected by `RATATOSKR_CONFIG`, back up that file instead.
Review archive contents and encrypt this material before it leaves the host.

The nested Git-mirror volume is not captured by the host `data/` archive. Copy it
through an application container that mounts both locations:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml run --rm --no-deps \
  --entrypoint sh ratatoskr -c \
  'tar -C /data -czf /data/git-mirrors-backup.tar.gz git-mirrors'

mv data/git-mirrors-backup.tar.gz "$BACKUP_DIR/"
```

If all mirrors can be recreated from still-available remotes, recording that
decision is an acceptable alternative to copying the volume.

### Qdrant

Do not archive a guessed `qdrant_data/` host directory: the default is a Compose
named volume. Either use Qdrant's collection snapshot API and store the downloaded
snapshot with the backup, or treat Qdrant as derived and rebuild it after restore.

Create a collection snapshot with the Qdrant API when exact vector recovery is
required:

```bash
curl --fail -X POST \
  http://127.0.0.1:6333/collections/summaries/snapshots
```

Collection names can vary with environment/version configuration. Confirm the
active name before snapshotting.

### Manifest and restart

```bash
(cd "$BACKUP_DIR" && shasum -a 256 * > SHA256SUMS)
tar -tzf "$BACKUP_DIR/data.tar.gz" >/dev/null
tar -tzf "$BACKUP_DIR/config.tar.gz" >/dev/null

POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml up -d
```

Copy the backup and manifest off-host. Apply a retention policy only after at
least one restore rehearsal succeeds.

## Restore rehearsal

Restore into an isolated host or project name first. Never rehearse against the
only production database.

1. Provision the target revision and its Compose dependencies.
2. Verify `SHA256SUMS` and decrypt archives if necessary.
3. Restore configuration deliberately; do not overwrite newer secrets blindly.
4. Restore PostgreSQL into an empty database.
5. Restore application files and, if retained, the Git-mirror volume.
6. Apply reviewed Alembic migrations for the target application.
7. Restore a compatible Qdrant snapshot or rebuild the vector index.
8. Start services and verify old reads, new writes, search, and authentication.

### PostgreSQL restore

The following commands destroy the target database. Use them only on the chosen
restore target after confirming the dump and target identity:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml stop \
  ratatoskr worker scheduler mobile-api mcp mcp-write

POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml exec -T postgres \
  dropdb --force -U postgres ratatoskr

POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml exec -T postgres \
  createdb -U postgres -O ratatoskr_app ratatoskr

POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml exec -T postgres \
  pg_restore --no-owner --no-privileges \
  -U ratatoskr_app -d ratatoskr \
  < "$BACKUP_DIR/ratatoskr.dump"
```

Restore `data/` only after reviewing the archive for the intended destination:

```bash
tar -tzf "$BACKUP_DIR/data.tar.gz" | less
tar -C . -xzf "$BACKUP_DIR/data.tar.gz"
```

Restore the nested Git-mirror archive through the same mounted service:

```bash
cp "$BACKUP_DIR/git-mirrors-backup.tar.gz" data/
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml run --rm --no-deps \
  --entrypoint sh ratatoskr -c \
  'tar -C /data -xzf /data/git-mirrors-backup.tar.gz'
rm data/git-mirrors-backup.tar.gz
```

Apply and verify the target schema:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db --apply

POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml run --rm migrate \
  python -m app.cli.migrate_db --status
```

If no Qdrant snapshot was restored, start Qdrant and rebuild derived vectors:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml up -d qdrant postgres

POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml run --rm ratatoskr \
  python -m app.cli.backfill_vector_store --force
```

Finally start the deployment and verify:

```bash
POSTGRES_PASSWORD=... \
docker compose -f ops/docker/docker-compose.yml up -d

curl --fail http://127.0.0.1:18000/health
```

Confirm database counts, a known historical summary, a new end-to-end request,
semantic search, Telegram/session behavior, and any deployment-specific optional
integration. Record the rehearsal date, duration, artifact identifiers, and any
manual corrections.

## User-export encryption is separate

`BACKUP_ENCRYPTION_ENABLED` and `BACKUP_ENCRYPTION_KEY` control Fernet encryption
for per-user export archives. Restore upload limits are configured with
`BACKUP_RESTORE_MAX_UPLOAD_BYTES`, `BACKUP_MAX_ZIP_ENTRIES`,
`BACKUP_MAX_COMPRESSED_BYTES`, `BACKUP_MAX_DECOMPRESSED_BYTES`, and
`BACKUP_MAX_COMPRESSION_RATIO`.

That application-level format is distinct from the PostgreSQL sidecar's optional
OpenSSL encryption. Preserve the matching key and procedure for each artifact.

See also [Disaster Recovery Runbook](../runbooks/disaster-recovery.md) and
[Migrate Between Versions](migrate-versions.md).
