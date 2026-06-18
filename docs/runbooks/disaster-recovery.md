# Disaster Recovery Runbook

Use this runbook when the self-hosted Ratatoskr instance loses a host, corrupts a datastore, or needs a quarterly restore drill. Keep secret values, backup passphrases, dump contents, and customer/user data out of issues and chat; record only timestamps, artifact names, checksums, counts, owners, and verification results.

## Recovery Targets

| Target | Value | Rationale |
|---|---:|---|
| RTO | 1 hour | Single-user self-hosted deployment can tolerate a short manual restore window, but Telegram/API access should return the same day. |
| RPO | 24 hours | The `pg-backup` sidecar creates one PostgreSQL dump per day by default (`BACKUP_CRON=0 3 * * *`). |
| Drill cadence | Quarterly | Open `.github/ISSUE_TEMPLATE/disaster-recovery-drill.md`, execute the checklist against a disposable host/database, and append the result to the sign-off table below. |

## Scope

Authoritative state lives in PostgreSQL. Qdrant can be restored from a volume snapshot or rebuilt from PostgreSQL embeddings. Redis is operational state only; restore Redis AOF when you need to preserve queued Taskiq jobs, digest session state, or short-lived rate/session keys, otherwise start empty and accept reprocessing/re-login.

## Activation Criteria

- `ratatoskr-postgres` cannot start because the volume is missing or corrupt.
- Postgres starts but migrations, healthcheck, or core read paths fail due to schema/data corruption.
- Host disk or SD-card failure requires moving to a new machine.
- A quarterly restore drill is due.

## Roles

| Role | Responsibility |
|---|---|
| Operator | Owns commands, verifies backups, restores data, and records evidence. |
| Reviewer | Watches for destructive mistakes, validates counts/checksums, and signs off. |
| Communicator | Sends user-facing downtime and recovery updates. For a single-user deployment this can be the same person as the operator. |

## Communication Templates

Start of incident:

```text
Ratatoskr is in recovery mode. Telegram ingestion and API access may be unavailable while I restore the database from the newest verified backup. Next update by <time>.
```

Recovery complete:

```text
Ratatoskr is back online. Restore source: <backup artifact timestamp>. Verification completed: Postgres counts, latest summary timestamp, Qdrant search/collection check, Redis policy check. Known gaps: <none/list>.
```

Rollback or failed restore:

```text
Ratatoskr restore did not pass verification. I am keeping write traffic stopped and switching to the previous recovery point / opening follow-up investigation. Next update by <time>.
```

## Pre-Restore Decision

1. Freeze writers: `docker compose -f ops/docker/docker-compose.yml stop ratatoskr worker scheduler mobile-api mcp mcp-write`.
2. Keep the failed state until you have a safety copy: dump the damaged Postgres if it is reachable, copy Qdrant/Redis volumes if they exist, and snapshot `.env`.
3. Pick the newest backup whose metadata JSON has a matching `sha256` and whose timestamp satisfies the 24h RPO target.
4. Confirm whether the dump is encrypted. If it is `.dump.enc`, load the `BACKUP_ENCRYPTION_KEY` that created that artifact from the external secret store before decrypting.
5. Decide Qdrant strategy: restore snapshot for fastest recovery, or rebuild vectors after Postgres is healthy if Qdrant state is missing or suspicious.
6. Decide Redis strategy: restore AOF only when preserving queued jobs/session TTLs matters more than a clean restart.

## PostgreSQL Restore

Set the artifact variables on the host:

```bash
export BACKUP_HOST_DIR="${BACKUP_HOST_DIR:-data/postgres-backups}"
export BACKUP_TS=YYYYMMDDTHHMMSSZ
export PG_DUMP="$BACKUP_HOST_DIR/ratatoskr-postgres-$BACKUP_TS.dump"
export PG_DUMP_ENC="$BACKUP_HOST_DIR/ratatoskr-postgres-$BACKUP_TS.dump.enc"
export PG_META="$BACKUP_HOST_DIR/ratatoskr-postgres-$BACKUP_TS.json"
```

Verify metadata before touching the live database:

```bash
cat "$PG_META"
sha256sum "$PG_DUMP" 2>/dev/null || sha256sum "$PG_DUMP_ENC"
```

Reset the target database:

```bash
docker compose -f ops/docker/docker-compose.yml up -d postgres
docker exec -i ratatoskr-postgres psql -U postgres -v ON_ERROR_STOP=1 -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'ratatoskr';"
docker exec -i ratatoskr-postgres psql -U postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS ratatoskr;"
docker exec -i ratatoskr-postgres psql -U postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE ratatoskr OWNER ratatoskr_app;"
```

Restore an unencrypted dump:

```bash
docker exec -i ratatoskr-postgres pg_restore --no-owner --no-privileges --clean --if-exists -U ratatoskr_app -d ratatoskr < "$PG_DUMP"
```

Restore an encrypted dump:

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -pass env:BACKUP_ENCRYPTION_KEY -in "$PG_DUMP_ENC" | docker exec -i ratatoskr-postgres pg_restore --no-owner --no-privileges --clean --if-exists -U ratatoskr_app -d ratatoskr
```

Run migrations for the image you are about to start:

```bash
docker compose -f ops/docker/docker-compose.yml run --rm migrate
```

## Qdrant Restore Or Rebuild

Restore a Qdrant volume snapshot when one exists and belongs to the same restore point:

```bash
docker compose -f ops/docker/docker-compose.yml stop qdrant
rm -rf qdrant_data
tar -C . -xzf "$BACKUP_HOST_DIR/qdrant_data-$BACKUP_TS.tar.gz"
docker compose -f ops/docker/docker-compose.yml up -d qdrant
```

If Qdrant is missing, stale, or not trusted, rebuild from PostgreSQL after Postgres is healthy:

```bash
docker compose -f ops/docker/docker-compose.yml up -d qdrant
DATABASE_URL="${DATABASE_URL:?set DATABASE_URL}" python -m app.cli.backfill_vector_store --force
DATABASE_URL="${DATABASE_URL:?set DATABASE_URL}" python -m app.cli.reconcile_vector_index --repair
```

## Redis Restore Or Reset

The default Redis service uses AOF persistence for operational state. Restore AOF only when preserving queue/session state is required:

```bash
docker compose -f ops/docker/docker-compose.yml stop redis
rm -rf redis_data
tar -C . -xzf "$BACKUP_HOST_DIR/redis_data-$BACKUP_TS.tar.gz"
docker compose -f ops/docker/docker-compose.yml up -d redis
```

For a clean reset, start Redis empty and expect expired sessions, replayed jobs, or manual digest/session reinitialization:

```bash
docker compose -f ops/docker/docker-compose.yml up -d redis
```

## Verification Checklist

Run these checks before restarting writers:

```bash
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT COUNT(*) AS requests FROM requests;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT COUNT(*) AS summaries FROM summaries;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT MAX(created_at) AS latest_summary_created_at FROM summaries;"
docker exec -i ratatoskr-postgres psql -U ratatoskr_app -d ratatoskr -c "SELECT version_num FROM alembic_version;"
curl -fsS http://localhost:6333/collections | jq .
docker exec -i ratatoskr-redis redis-cli DBSIZE
docker compose -f ops/docker/docker-compose.yml run --rm ratatoskr python -m app.cli.healthcheck
```

Expected results:

- Postgres row counts are plausible compared with the latest known pre-incident counts or previous drill evidence.
- `latest_summary_created_at` is no older than the selected backup timestamp plus the accepted RPO gap.
- Alembic reports the current head for the checked-out image.
- Qdrant collection sizes match the restored snapshot or the rebuild/reconcile report.
- Redis `DBSIZE` is either expected from restored AOF or intentionally low after reset.
- Healthcheck exits 0 and logs contain no repeated database, migration, or vector errors.

## Restart

After verification passes:

```bash
docker compose -f ops/docker/docker-compose.yml up -d redis qdrant mobile-api worker scheduler ratatoskr
docker compose -f ops/docker/docker-compose.yml logs --tail=200 ratatoskr worker mobile-api
```

Watch for at least 10 minutes. Confirm Telegram owner commands work, mobile/API health endpoints answer, semantic search either works or is explicitly degraded while Qdrant rebuild continues, and no background job loop is repeatedly failing.

## Backup Encryption Key Rotation During Restore

Encrypted backup archives require the key that encrypted them. During an in-flight restore:

1. Do not rotate `BACKUP_ENCRYPTION_KEY` until the chosen archive has been decrypted and verified.
2. If rotation is required because the key may be compromised, keep the old key in the secret manager until every archive encrypted with it expires or is re-encrypted.
3. Restore with the old key, verify the database, then set the new `BACKUP_ENCRYPTION_KEY`.
4. Run `docker compose -f ops/docker/docker-compose.yml exec pg-backup ratatoskr-pg-backup-run` to create a fresh backup under the new key.
5. Verify the new encrypted dump with `pg_restore --list` before deleting any old-key material.

## Quarterly Drill

1. Open `.github/ISSUE_TEMPLATE/disaster-recovery-drill.md`.
2. Run `tools/scripts/restore_smoke.sh tests/fixtures/restore_smoke.dump` against a disposable Postgres 16 service.
3. Exercise the manual Postgres restore commands against the newest real backup in a non-production database or host.
4. Choose Qdrant restore or rebuild and record collection counts.
5. Choose Redis restore or reset and record the reason.
6. Fill in RTO/RPO observations, command outputs, artifact timestamp, metadata SHA256, and follow-ups in the issue.
7. Append a row to the sign-off table below.

## Drill Sign-Off

| Date | Mode | Operator | Reviewer | Artifact | Result | Evidence |
|---|---|---|---|---|---|---|
| 2026-06-18 | CI restore smoke fixture | Codex | Pending human reviewer | `tests/fixtures/restore_smoke.dump` | Pass | `tools/scripts/restore_smoke.sh`; local disposable Postgres restore smoke |
