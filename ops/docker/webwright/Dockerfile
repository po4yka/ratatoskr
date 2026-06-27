# Webwright sidecar — wraps Microsoft Webwright (https://github.com/microsoft/Webwright)
# behind a thin FastAPI HTTP contract so Ratatoskr's scraper chain and `/browse`
# command can invoke it over the internal compose network.
#
# Webwright has no published releases yet. Pin by upstream commit SHA via the
# `WEBWRIGHT_REF` build arg; the default below should be advanced as upstream
# stabilizes. The bundled Microsoft Playwright image ships Chromium + system
# deps so we don't reinstall them ourselves.

FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

ARG WEBWRIGHT_REPO=https://github.com/microsoft/Webwright.git
# Pinned to an immutable upstream commit SHA (NOT a moving branch like `main`)
# so the build is reproducible and cannot pull in an attacker's commit pushed to
# the upstream default branch. Bump this SHA deliberately after reviewing the
# upstream diff.
ARG WEBWRIGHT_REF=4a46f282ec37f27d6003cc498a977939d62d9015

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEBWRIGHT_OUTPUTS_DIR=/data/webwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# Clone Webwright and check out the pinned commit SHA. A full clone (not
# --depth 1) is required so an arbitrary historical SHA can be checked out. The
# checkout MUST hard-fail the build if the pinned SHA is unavailable — never
# silently fall back to the default branch (that would defeat the pin).
RUN git clone "${WEBWRIGHT_REPO}" Webwright \
 && git -C Webwright checkout "${WEBWRIGHT_REF}"

# Editable install is best-effort: upstream is pre-release and dependency pins
# shift, so the sidecar declares its narrow surface in requirements.txt and
# resolves the rest from the cloned tree on PYTHONPATH.
RUN pip install --no-cache-dir -e ./Webwright \
 || echo "[webwright] editable install failed; falling back to PYTHONPATH"

ENV PYTHONPATH=/opt/Webwright/src:${PYTHONPATH:-}

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

RUN mkdir -p ${WEBWRIGHT_OUTPUTS_DIR}

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8090/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090", "--log-level", "info"]
