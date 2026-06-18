#!/bin/sh
set -eu

log() {
  printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

write_metric() {
  metric_dir="${BACKUP_METRICS_DIR:-/var/lib/node-exporter/textfile_collector}"
  mkdir -p "$metric_dir"
  tmp_metric="$metric_dir/ratatoskr_pg_backup.prom.$$"
  {
    printf '# HELP ratatoskr_pg_backup_last_success_timestamp_seconds Unix timestamp of the last successful automated PostgreSQL backup.\n'
    printf '# TYPE ratatoskr_pg_backup_last_success_timestamp_seconds gauge\n'
    printf 'ratatoskr_pg_backup_last_success_timestamp_seconds %s\n' "$1"
    printf '# HELP ratatoskr_pg_backup_last_success_size_bytes Size in bytes of the last successful automated PostgreSQL backup artifact.\n'
    printf '# TYPE ratatoskr_pg_backup_last_success_size_bytes gauge\n'
    printf 'ratatoskr_pg_backup_last_success_size_bytes %s\n' "$2"
    printf '# HELP ratatoskr_pg_backup_last_failure_timestamp_seconds Unix timestamp of the last failed automated PostgreSQL backup attempt, or 0 if none recorded by this container.\n'
    printf '# TYPE ratatoskr_pg_backup_last_failure_timestamp_seconds gauge\n'
    printf 'ratatoskr_pg_backup_last_failure_timestamp_seconds %s\n' "${3:-0}"
  } > "$tmp_metric"
  mv "$tmp_metric" "$metric_dir/ratatoskr_pg_backup.prom"
}

write_failure_metric() {
  metric_dir="${BACKUP_METRICS_DIR:-/var/lib/node-exporter/textfile_collector}"
  mkdir -p "$metric_dir"
  previous_success="0"
  previous_size="0"
  if [ -f "$metric_dir/ratatoskr_pg_backup.prom" ]; then
    previous_success="$(awk '/^ratatoskr_pg_backup_last_success_timestamp_seconds / {print $2}' "$metric_dir/ratatoskr_pg_backup.prom" | tail -1)"
    previous_size="$(awk '/^ratatoskr_pg_backup_last_success_size_bytes / {print $2}' "$metric_dir/ratatoskr_pg_backup.prom" | tail -1)"
  fi
  write_metric "${previous_success:-0}" "${previous_size:-0}" "$(date -u '+%s')"
}

cleanup_tmp() {
  rm -f "${tmp_dump:-}" "${tmp_artifact:-}" "${tmp_meta:-}"
}

fail() {
  log "pg_backup_failed error=$(json_escape "$1")"
  write_failure_metric
  cleanup_tmp
  exit 1
}

backup_dir="${BACKUP_DIR:-/backups}"
metrics_dir="${BACKUP_METRICS_DIR:-/var/lib/node-exporter/textfile_collector}"
retention_days="${BACKUP_RETENTION_DAYS:-14}"
postgres_host="${POSTGRES_HOST:-postgres}"
postgres_port="${POSTGRES_PORT:-5432}"
postgres_db="${POSTGRES_DB:-ratatoskr}"
postgres_user="${POSTGRES_USER:-ratatoskr_app}"
timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
base_name="ratatoskr-postgres-$timestamp"
lock_dir="/tmp/ratatoskr-pg-backup.lock"

case "$retention_days" in
  ''|*[!0-9]*) fail "BACKUP_RETENTION_DAYS must be a non-negative integer" ;;
esac

mkdir -p "$backup_dir" "$metrics_dir"

if ! mkdir "$lock_dir" 2>/dev/null; then
  log "pg_backup_skipped reason=lock_held"
  exit 0
fi
trap 'rmdir "$lock_dir" 2>/dev/null || true; cleanup_tmp' EXIT INT TERM

tmp_dump="$backup_dir/$base_name.dump.tmp"
tmp_artifact="$backup_dir/$base_name.artifact.tmp"
tmp_meta="$backup_dir/$base_name.json.tmp"
dump_path="$backup_dir/$base_name.dump"
artifact_path="$dump_path"
encrypted="false"

log "pg_backup_started host=$postgres_host db=$postgres_db target=$backup_dir"

PGPASSWORD="${POSTGRES_PASSWORD:-}" pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --host="$postgres_host" \
  --port="$postgres_port" \
  --username="$postgres_user" \
  --dbname="$postgres_db" \
  --file="$tmp_dump" || fail "pg_dump failed"

if [ -n "${BACKUP_ENCRYPTION_KEY:-}" ]; then
  encrypted="true"
  artifact_path="$backup_dir/$base_name.dump.enc"
  openssl enc -aes-256-cbc -pbkdf2 -salt \
    -pass env:BACKUP_ENCRYPTION_KEY \
    -in "$tmp_dump" \
    -out "$tmp_artifact" || fail "backup encryption failed"
  rm -f "$tmp_dump"
  mv "$tmp_artifact" "$artifact_path"
else
  mv "$tmp_dump" "$artifact_path"
fi

size_bytes="$(wc -c < "$artifact_path" | tr -d ' ')"
sha256="$(sha256sum "$artifact_path" | awk '{print $1}')"
success_ts="$(date -u '+%s')"
metadata_path="$backup_dir/$base_name.json"

cat > "$tmp_meta" <<EOF
{
  "timestamp": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "database": "$(json_escape "$postgres_db")",
  "host": "$(json_escape "$postgres_host")",
  "artifact": "$(json_escape "$(basename "$artifact_path")")",
  "format": "pg_dump_custom",
  "encrypted": $encrypted,
  "size_bytes": $size_bytes,
  "sha256": "$sha256"
}
EOF
mv "$tmp_meta" "$metadata_path"

if [ -n "${BACKUP_S3_BUCKET:-}" ]; then
  s3_prefix="${BACKUP_S3_PREFIX:-ratatoskr/postgres}"
  s3_uri="s3://$BACKUP_S3_BUCKET/$s3_prefix/$base_name"
  export AWS_ACCESS_KEY_ID="${BACKUP_S3_ACCESS_KEY:-${AWS_ACCESS_KEY_ID:-}}"
  export AWS_SECRET_ACCESS_KEY="${BACKUP_S3_SECRET_KEY:-${AWS_SECRET_ACCESS_KEY:-}}"
  export AWS_DEFAULT_REGION="${BACKUP_S3_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
  endpoint_args=""
  if [ -n "${BACKUP_S3_ENDPOINT_URL:-}" ]; then
    endpoint_args="--endpoint-url ${BACKUP_S3_ENDPOINT_URL}"
  fi
  # shellcheck disable=SC2086
  aws $endpoint_args s3 cp "$artifact_path" "$s3_uri/$(basename "$artifact_path")" || fail "artifact upload failed"
  # shellcheck disable=SC2086
  aws $endpoint_args s3 cp "$metadata_path" "$s3_uri/$(basename "$metadata_path")" || fail "metadata upload failed"
fi

if [ "$retention_days" -gt 0 ]; then
  find "$backup_dir" -maxdepth 1 -type f -name 'ratatoskr-postgres-*' -mtime "+$retention_days" -delete
fi

write_metric "$success_ts" "$size_bytes" "0"
log "pg_backup_completed artifact=$(basename "$artifact_path") size_bytes=$size_bytes sha256=$sha256 encrypted=$encrypted"
