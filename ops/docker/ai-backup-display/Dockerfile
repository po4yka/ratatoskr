FROM debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        openbox \
        procps \
        tigervnc-standalone-server \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY ops/docker/ai-backup-display/entrypoint.sh /usr/local/bin/ai-backup-display
COPY ops/docker/ai-backup-display/healthcheck.sh /usr/local/bin/ai-backup-display-healthcheck.sh
COPY ops/docker/ai-backup-display/openbox-rc.xml /etc/xdg/openbox/ratatoskr-rc.xml

RUN chmod 0755 /usr/local/bin/ai-backup-display /usr/local/bin/ai-backup-display-healthcheck.sh

EXPOSE 5900
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/ai-backup-display"]
