FROM debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        dbus-bin \
        tini \
        xdg-dbus-proxy \
    && rm -rf /var/lib/apt/lists/*

COPY ops/docker/ai-backup-webauthn-bridge/entrypoint.sh /usr/local/bin/ai-backup-webauthn-bridge
COPY ops/docker/ai-backup-webauthn-bridge/healthcheck.sh /usr/local/bin/ai-backup-webauthn-healthcheck.sh

RUN chmod 0755 \
    /usr/local/bin/ai-backup-webauthn-bridge \
    /usr/local/bin/ai-backup-webauthn-healthcheck.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/ai-backup-webauthn-bridge"]
