#!/usr/bin/env bash
set -euo pipefail

dump_path="${1:-tests/fixtures/restore_smoke.dump}"
db_name="${RESTORE_SMOKE_DB:-ratatoskr_restore_smoke}"
pg_host="${PGHOST:-localhost}"
pg_port="${PGPORT:-5432}"
pg_user="${PGUSER:-postgres}"
pg_password="${PGPASSWORD:-postgres}"
restore_database_url="${RESTORE_SMOKE_DATABASE_URL:-postgresql+asyncpg://${pg_user}:${pg_password}@${pg_host}:${pg_port}/${db_name}}"

if [[ ! -f "$dump_path" ]]; then
  echo "restore smoke dump does not exist: $dump_path" >&2
  exit 64
fi

echo "Preparing restore smoke database: ${db_name}"
dropdb --if-exists "$db_name"
createdb "$db_name"

echo "Restoring sample pg_dump archive: ${dump_path}"
pg_restore \
  --exit-on-error \
  --no-owner \
  --no-privileges \
  --dbname="$db_name" \
  "$dump_path"

seed_count="$(psql -d "$db_name" -Atc "SELECT COUNT(*) FROM restore_smoke_seed WHERE label = 'ratatoskr restore smoke fixture';")"
if [[ "$seed_count" != "1" ]]; then
  echo "restore smoke seed verification failed: expected 1 row, got ${seed_count}" >&2
  exit 1
fi

echo "Running Alembic migrations against restored database"
DATABASE_URL="$restore_database_url" python -m app.cli.migrate_db --apply

alembic_rows="$(psql -d "$db_name" -Atc "SELECT COUNT(*) FROM alembic_version;")"
if [[ "$alembic_rows" != "1" ]]; then
  echo "migration verification failed: expected 1 alembic_version row, got ${alembic_rows}" >&2
  exit 1
fi

post_migration_seed_count="$(psql -d "$db_name" -Atc "SELECT COUNT(*) FROM restore_smoke_seed WHERE label = 'ratatoskr restore smoke fixture';")"
if [[ "$post_migration_seed_count" != "1" ]]; then
  echo "post-migration seed verification failed: expected 1 row, got ${post_migration_seed_count}" >&2
  exit 1
fi

echo "Restore smoke succeeded for ${dump_path}"
