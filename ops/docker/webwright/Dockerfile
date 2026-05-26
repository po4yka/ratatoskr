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
# TODO: bump this SHA as upstream stabilizes.
ARG WEBWRIGHT_REF=main

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEBWRIGHT_OUTPUTS_DIR=/data/webwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# Clone Webwright at the pinned ref. We do NOT `pip install -e .` blindly —
# upstream is pre-release and dependency pins shift; the sidecar declares the
# narrow surface it needs in requirements.txt and lets the agent's runtime
# imports resolve from the cloned tree on PYTHONPATH.
RUN git clone --depth 1 "${WEBWRIGHT_REPO}" Webwright \
 && cd Webwright \
 && (git fetch --depth 1 origin "${WEBWRIGHT_REF}" && git checkout FETCH_HEAD || true) \
 && pip install --no-cache-dir -e . || echo "[webwright] editable install failed; falling back to PYTHONPATH"

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
