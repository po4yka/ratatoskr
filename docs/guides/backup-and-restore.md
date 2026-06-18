# Back Up and Restore

Protect a Ratatoskr instance by backing up the durable host paths and the PostgreSQL database used by the current Docker Compose deployment.

**Audience:** Operators **Difficulty:** Intermediate **Estimated Time:** 20 minutes

---

## Scope

The default Compose file is `ops/docker/docker-compose.yml`. Run these commands from the repository root.

Durable data is split across these locations:

| Data | Location | Source |
| ---- | -------- | ------ |
| PostgreSQL database | `ratatoskr-postgres` Compose service (`postgres_data` volume) | `DATABASE_URL` |
| Qdrant vector store | `qdrant_data/` on the host, `/qdrant/storage` in the Qdrant container | Qdrant volume mount |
| YouTube downloads | `data/videos/` | `YOUTUBE_STORAGE_PATH` default `/data/videos` |
| Attachments and non-YouTube media | `data/attachments/`, `data/video-sources/` | attachment defaults |
| TTS audio cache | `data/audio/` | `ELEVENLABS_AUDIO_PATH` default `/data/audio` |
| Telethon session files | `data/sessions/` | digest userbot session, plus a `.legacy.bak.session` after the Phase 6 cutover |
| Config and secrets | `.env`, `ratatoskr.yaml`, `config/ratatoskr.yaml`, `config/models.yaml` when present | config search order |
| Redis | no durable backup expected in default Compose | `redis-server --save "" --appendonly no` |

The API `/v1/backups` and Telegram `/backup` flows create per-user export ZIPs under `data/backups/<user_id>/`. They are useful for user data export, but they are not a full instance backup because they do not include Qdrant, media files, all operational tables, or config.

---

## Backup Encryption

The per-user export ZIPs created by the `/v1/backups` API and the Telegram `/backup` command can be encrypted at rest using Fernet (AES-128-CBC + HMAC-SHA256). The `cryptography` package that ships with Ratatoskr provides this without extra dependencies.

### Generating A Key

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the 44-character output into `.env`:

```bash
BACKUP_ENCRYPTION_KEY=<your-44-char-key>
```

Encryption is enabled automatically when `BACKUP_ENCRYPTION_KEY` is present. Encrypted files are written with a `.zip.enc` extension; unencrypted files keep `.zip`.

### Disabling Encryption Explicitly

Set `BACKUP_ENCRYPTION_ENABLED=false` to skip encryption even when a key is configured — for example, during a migration window:

```bash
BACKUP_ENCRYPTION_ENABLED=false
```

This does not affect instance-level backups (PostgreSQL, Qdrant, media) described in this guide.

### Restoring Encrypted Archives

The restore endpoint auto-detects whether an uploaded archive is encrypted and decrypts it before extraction. Provide the same `BACKUP_ENCRYPTION_KEY` that was active when the backup was created. Old plaintext `.zip` archives remain restorable without a key.

If the wrong key is active the restore returns without touching the database:

```json
{ "errors": ["Could not decrypt backup (wrong key or corrupted archive)"] }
```

If encryption was enabled when the backup was created but `BACKUP_ENCRYPTION_KEY` is missing on restore:

```json
{ "errors": ["Encrypted backup but BACKUP_ENCRYPTION_KEY is not configured"] }
```

### Safety Limits

The restore endpoint validates ZIP metadata before extracting any bytes. These limits are configurable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `BACKUP_MAX_RESTORE_BYTES` | 100 MB | Upload size gate (returns 413 before reading body) |
| `BACKUP_MAX_ZIP_ENTRIES` | 100 | Maximum number of entries in the archive |
| `BACKUP_MAX_COMPRESSED_BYTES` | 100 MB | Maximum sum of compressed entry sizes |
| `BACKUP_MAX_DECOMPRESSED_BYTES` | 500 MB | Maximum sum of uncompressed entry sizes |
| `BACKUP_MAX_COMPRESSION_RATIO` | 100 | Per-entry ratio cap (zip bomb guard) |

---

## Before You Start

Set a backup timestamp once so every archive has matching names:

```bash
export BACKUP_TS="$(date -u +%Y%m%dT%H%M%SZ)"
export BACKUP_DIR="backups/$BACKUP_TS"
mkdir -p "$BACKUP_DIR"
```

Check the effective Compose services:

```bash
docker compose -f ops/docker/docker-compose.yml ps
```

For the most consistent backup, stop services that can write to Postgres or Qdrant:

```bash
docker compose -f ops/docker/docker-compose.yml stop ratatoskr mobile-api mcp mcp-write qdrant
```

`pg_dump` is online-safe (it takes a transactionally consistent snapshot without blocking writers), so the Postgres container can keep running for the database section below; only stop everything together if you also need to capture Qdrant + media at the same logical point in time.

---

## Backup

### Automated PostgreSQL Sidecar

Production Compose includes a `pg-backup` sidecar that runs `pg_dump --format=custom` against the `postgres` service on `BACKUP_CRON` (`0 3 * * *` UTC by default). It writes artifacts to the host bind mount `BACKUP_HOST_DIR` (`data/postgres-backups` when running the primary Compose file from the repository root) and keeps local files for `BACKUP_RETENTION_DAYS` days (`14` by default).

Start or refresh the sidecar with the rest of the stack:

```bash
POSTGRES_PASSWORD=... docker compose -f ops/docker/docker-compose.yml up -d postgres pg-backup
```

Run one backup immediately after configuration changes:

```bash
POSTGRES_PASSWORD=... docker compose -f ops/docker/docker-compose.yml exec pg-backup ratatoskr-pg-backup-run
```

Each successful run creates a `ratatoskr-postgres-<timestamp>.dump` file, or `.dump.enc` when `BACKUP_ENCRYPTION_KEY` is set, plus a sibling `.json` metadata file with `timestamp`, `size_bytes`, and `sha256`. Inspect the local backup directory from the host:

```bash
ls -lh "${BACKUP_HOST_DIR:-data/postgres-backups}"
cat "${BACKUP_HOST_DIR:-data/postgres-backups}"/ratatoskr-postgres-*.json | tail -1
```

Verify an unencrypted automated dump:

```bash
docker run --rm -i --entrypoint pg_restore postgres:16 --list < "${BACKUP_HOST_DIR:-data/postgres-backups}/ratatoskr-postgres-YYYYMMDDTHHMMSSZ.dump" | head
```

Verify an encrypted automated dump with the same passphrase that created it:

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_ENCRYPTION_KEY -in "${BACKUP_HOST_DIR:-data/postgres-backups}/ratatoskr-postgres-YYYYMMDDTHHMMSSZ.dump.enc" | docker run --rm -i --entrypoint pg_restore postgres:16 --list | head
```

Set `BACKUP_S3_BUCKET` to upload each dump and metadata file after local creation. For S3-compatible storage such as Backblaze B2 or MinIO, also set `BACKUP_S3_ENDPOINT_URL`; credentials come from `BACKUP_S3_ACCESS_KEY` / `BACKUP_S3_SECRET_KEY` or the corresponding AWS CLI environment variables inside the sidecar.

The sidecar writes node-exporter textfile metrics to the shared `pg_backup_metrics` volume. Prometheus scrapes `ratatoskr_pg_backup_last_success_timestamp_seconds`, and the `RatatoskrPostgresBackupStale` critical alert fires when the metric is missing or older than 36 hours. Restore rehearsals are tracked through `docs/runbooks/disaster-recovery.md`; the restore commands below are the canonical manual path.

### PostgreSQL

Run `pg_dump` inside the `ratatoskr-postgres` container and stream the dump out to the host:

```bash
docker exec -t ratatoskr-postgres \
  pg_dump --format=custom --no-owner --no-privileges \
          -U ratatoskr_app -d ratatoskr \
  > "$BACKUP_DIR/ratatoskr.dump"
```

Verify the dump is well-formed:

```bash
docker run --rm -i --entrypoint pg_restore postgres:16 \
  --list < "$BACKUP_DIR/ratatoskr.dump" | head
```

For a plain-text dump (larger but human-readable):

```bash
docker exec -t ratatoskr-postgres \
  pg_dumpall -U ratatoskr_app --no-owner --no-privileges \
  > "$BACKUP_DIR/ratatoskr.sql"
```

### Qdrant

The default Qdrant service persists its database through the host bind mount `qdrant_data:/qdrant/storage`. Back it up after stopping `qdrant`:

```bash
tar -C . -czf "$BACKUP_DIR/qdrant_data.tar.gz" qdrant_data
```

Alternatively, use Qdrant's native snapshot API (can run while Qdrant is live):

```bash
curl -X POST http://localhost:6333/collections/summaries/snapshots
# Download the snapshot file from /collections/summaries/snapshots/{snapshot_name}
```

Qdrant data is rebuildable from Postgres for summaries that have enough stored text and embedding inputs:

```bash
python -m app.cli.backfill_vector_store --force
```

Backing up `qdrant_data/` is still faster and preserves the exact current vector store. Rebuild when the archive is missing, corrupted, or intentionally stale after an embedding model or namespace change.

### Redis

Default Redis is internal-only and configured without RDB or AOF persistence:

```yaml
command: ["redis-server", "--save", "", "--appendonly", "no"]
```

Do not expect Redis data to survive container recreation, and do not include the `redis_data` volume in release-critical backups. Ratatoskr uses Redis for ephemeral caches, auth/sync/session TTLs, rate-limit state, batch progress, and similar recoverable data. After restore, users may need to sign in again, sync sessions may be gone, and caches will warm naturally.

If you run an external persistent Redis with custom settings, use that deployment's normal `BGSAVE`, AOF, or managed snapshot process. That is outside the default Ratatoskr Compose contract.

### Media Files

Back up media directories that exist. The `video_downloads` table stores paths to downloaded YouTube videos, subtitles, and thumbnails, so restoring the files keeps cached video results usable.

```bash
for path in data/videos data/attachments data/video-sources data/audio; do
  if [ -d "$path" ]; then
    tar -C . -czf "$BACKUP_DIR/$(echo "$path" | tr / _).tar.gz" "$path"
  fi
done
```

Notes:

- `data/videos/` can be large. It is optional if you accept re-downloading or losing cached video files.
- `data/attachments/` and `data/video-sources/` are temporary by default, but include them for a byte-for-byte instance restore.
- `data/audio/` is a cache for generated audio and can be regenerated if the provider and source text are still available.

### Config And Secrets

Back up config files separately from the database. These files may contain API keys and should be encrypted at rest.

```bash
CONFIG_FILES=()
for path in .env ratatoskr.yaml config/ratatoskr.yaml config/models.yaml; do
  [ -f "$path" ] && CONFIG_FILES+=("$path")
done
[ "${#CONFIG_FILES[@]}" -gt 0 ] && tar -C . -czf "$BACKUP_DIR/config.tar.gz" "${CONFIG_FILES[@]}"
```

For a single encrypted archive:

```bash
tar -C backups -czf - "$BACKUP_TS" | \
  openssl enc -aes-256-cbc -pbkdf2 -salt -out "backups/$BACKUP_TS.tar.gz.enc"
```

Store the passphrase outside the host. Do not commit backup archives or copied `.env` files.

### Verify The Backup

```bash
find "$BACKUP_DIR" -maxdepth 1 -type f -print -exec ls -lh {} \;
docker run --rm -i --entrypoint pg_restore postgres:16 --list \
  < "$BACKUP_DIR/ratatoskr.dump" >/dev/null
[ ! -f "$BACKUP_DIR/qdrant_data.tar.gz" ] || tar -tzf "$BACKUP_DIR/qdrant_data.tar.gz" >/dev/null
[ ! -f "$BACKUP_DIR/config.tar.gz" ] || tar -tzf "$BACKUP_DIR/config.tar.gz" >/dev/null
```

Restart services after the backup:

```bash
docker compose -f ops/docker/docker-compose.yml up -d
```

---

## Restore

### Restore On The Same Host

Stop all services that can read or write restored state:

```bash
docker compose -f ops/docker/docker-compose.yml stop ratatoskr mobile-api mcp mcp-write qdrant redis
```

Keep a pre-restore copy of the current state:

```bash
mkdir -p "restore-safety/$BACKUP_TS"
docker exec -t ratatoskr-postgres \
  pg_dump --format=custom --no-owner --no-privileges \
          -U ratatoskr_app -d ratatoskr \
  > "restore-safety/$BACKUP_TS/ratatoskr.before-restore.dump"
[ -d qdrant_data ] && tar -C . -czf "restore-safety/$BACKUP_TS/qdrant_data.before-restore.tar.gz" qdrant_data
```

Restore PostgreSQL — drop and recreate the database, then load the dump:

```bash
docker exec -i ratatoskr-postgres \
  psql -U postgres -c "DROP DATABASE IF EXISTS ratatoskr;"
docker exec -i ratatoskr-postgres \
  psql -U postgres -c "CREATE DATABASE ratatoskr OWNER ratatoskr_app;"
docker exec -i ratatoskr-postgres \
  pg_restore --no-owner --no-privileges --clean --if-exists \
             -U ratatoskr_app -d ratatoskr \
  < "$BACKUP_DIR/ratatoskr.dump"
docker exec -i ratatoskr-postgres \
  psql -U ratatoskr_app -d ratatoskr -c "SELECT count(*) FROM requests;"
```

Restore Qdrant if you backed it up:

```bash
if [ -f "$BACKUP_DIR/qdrant_data.tar.gz" ]; then
  rm -rf qdrant_data
  tar -C . -xzf "$BACKUP_DIR/qdrant_data.tar.gz"
fi
```

Restore media archives that exist:

```bash
for archive in "$BACKUP_DIR"/data_*.tar.gz; do
  [ -e "$archive" ] || continue
  tar -C . -xzf "$archive"
done
```

Restore config files deliberately. Review before overwriting production secrets:

```bash
[ -f "$BACKUP_DIR/config.tar.gz" ] && tar -C . -xzf "$BACKUP_DIR/config.tar.gz"
```

Start the stack:

```bash
docker compose -f ops/docker/docker-compose.yml up -d
docker compose -f ops/docker/docker-compose.yml ps
```

Run migrations for the restored image if needed (Alembic upgrades are idempotent):

```bash
python -m app.cli.migrate_db --status
python -m app.cli.migrate_db
```

If Qdrant was not restored, rebuild it after Qdrant is healthy:

```bash
python -m app.cli.backfill_vector_store --force
```

### Restore To A New Host

On the new host:

```bash
git clone <repo-url> ratatoskr
cd ratatoskr
mkdir -p data config backups
cp <source>/.env .env  # or restore from config.tar.gz below
docker compose -f ops/docker/docker-compose.yml up -d ratatoskr-postgres qdrant
```

Copy the backup directory or encrypted archive to `backups/`, then load the database and unpack the rest:

```bash
export BACKUP_TS=YYYYMMDDTHHMMSSZ
export BACKUP_DIR="backups/$BACKUP_TS"

# Database
docker exec -i ratatoskr-postgres \
  psql -U postgres -c "CREATE DATABASE ratatoskr OWNER ratatoskr_app;" || true
docker exec -i ratatoskr-postgres \
  pg_restore --no-owner --no-privileges --clean --if-exists \
             -U ratatoskr_app -d ratatoskr \
  < "$BACKUP_DIR/ratatoskr.dump"

# Config + Qdrant + media
[ -f "$BACKUP_DIR/config.tar.gz" ]      && tar -C . -xzf "$BACKUP_DIR/config.tar.gz"
[ -f "$BACKUP_DIR/qdrant_data.tar.gz" ] && tar -C . -xzf "$BACKUP_DIR/qdrant_data.tar.gz"

for archive in "$BACKUP_DIR"/data_*.tar.gz; do
  [ -e "$archive" ] || continue
  tar -C . -xzf "$archive"
done
```

Validate and start:

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT 1;"
docker compose -f ops/docker/docker-compose.yml config
docker compose -f ops/docker/docker-compose.yml up -d
python -m app.cli.migrate_db --status
```

If the new host uses different paths, update `.env` or `ratatoskr.yaml` for `DATABASE_URL`, `YOUTUBE_STORAGE_PATH`, `ATTACHMENT_STORAGE_PATH`, `ATTACHMENT_VIDEO_STORAGE_PATH`, and `ELEVENLABS_AUDIO_PATH` before starting.

---

## Restore Test Checklist

Run this on a staging host or disposable VM before a release:

1. Create a full backup with the `pg_dump`, Qdrant, media, and config sections.
2. Restore it into an empty checkout following the "Restore To A New Host" flow.
3. Run `docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT 1;"`.
4. Run `docker compose -f ops/docker/docker-compose.yml config`.
5. Start the stack with `docker compose -f ops/docker/docker-compose.yml up -d`.
6. Confirm `ratatoskr`, `mobile-api`, `redis`, `ratatoskr-postgres`, and `qdrant` are healthy or intentionally disabled by profile/config.
7. Open the web/API and verify existing summaries are visible.
8. Run a semantic search. If Qdrant was rebuilt instead of restored, run `python -m app.cli.backfill_vector_store --force` to repopulate vectors from Postgres embeddings.
9. Open a restored YouTube summary with a `video_file_path` and confirm the file path exists under `data/videos/`, or accept that the media cache was not restored.
10. Send one known-good URL through the bot or CLI to confirm new writes work.

---

## Maintenance Commands

Reclaim space and update planner statistics:

```bash
docker exec -i ratatoskr-postgres \
  psql -U ratatoskr_app -d ratatoskr -c "VACUUM (ANALYZE);"
```

For a full rebuild that also reclaims indexed space (briefly locks each table):

```bash
docker exec -i ratatoskr-postgres \
  psql -U ratatoskr_app -d ratatoskr -c "VACUUM (FULL, ANALYZE);"
```

Check database size and table bloat:

```bash
docker exec -i ratatoskr-postgres \
  psql -U ratatoskr_app -d ratatoskr -c \
  "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
     FROM pg_catalog.pg_statio_user_tables
    ORDER BY pg_total_relation_size(relid) DESC
    LIMIT 20;"
```

If a database is unreachable or corrupted, follow the standard PostgreSQL recovery sequence: confirm the volume is intact, run `docker logs ratatoskr-postgres` for the underlying error, and restore the newest verified `pg_dump` archive using the steps in [Restore On The Same Host](#restore-on-the-same-host).

---

## See Also

- [Deployment](deploy-production.md)
- [How to Migrate Versions](migrate-versions.md)
- [Qdrant Vector Search](setup-qdrant-vector-search.md)
- [YouTube Downloads](configure-youtube-download.md)
