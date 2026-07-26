FROM postgres:17-alpine

RUN apk add --no-cache aws-cli bash ca-certificates coreutils openssl tzdata

COPY ops/docker/pg-backup/entrypoint.sh /usr/local/bin/ratatoskr-pg-backup-entrypoint
COPY ops/docker/pg-backup/run-backup.sh /usr/local/bin/ratatoskr-pg-backup-run

RUN chmod +x /usr/local/bin/ratatoskr-pg-backup-entrypoint /usr/local/bin/ratatoskr-pg-backup-run

ENTRYPOINT ["ratatoskr-pg-backup-entrypoint"]
