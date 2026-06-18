#!/bin/sh
set -eu

cron_expr="${BACKUP_CRON:-0 3 * * *}"
if [ "$(printf '%s\n' "$cron_expr" | awk '{print NF}')" -ne 5 ]; then
  echo "BACKUP_CRON must be a 5-field cron expression, got: $cron_expr" >&2
  exit 64
fi

mkdir -p "${BACKUP_DIR:-/backups}" "${BACKUP_METRICS_DIR:-/var/lib/node-exporter/textfile_collector}"

cat > /etc/crontabs/root <<EOF
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$cron_expr /usr/local/bin/ratatoskr-pg-backup-run >> /proc/1/fd/1 2>> /proc/1/fd/2
EOF

if [ "${BACKUP_RUN_ON_START:-false}" = "true" ]; then
  /usr/local/bin/ratatoskr-pg-backup-run
fi

exec crond -f -l "${BACKUP_CRON_LOG_LEVEL:-8}"
