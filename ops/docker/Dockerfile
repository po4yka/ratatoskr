# syntax=docker/dockerfile:1

# =============================================================================
# Stage 1: Builder - Install dependencies and compile wheels
# =============================================================================
FROM python:3.13-slim@sha256:2b7445fb71ca9cb15e9aab053fe8cb3162796f8e1d92ada12a49c766a811bc1e AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install build dependencies (will not be in final image)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       gcc \
       g++ \
       libxml2-dev \
       libxslt1-dev \
       zlib1g-dev \
       libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies with cache mounts for faster rebuilds
# Bot/MCP need: ml, youtube, export, scheduler, mcp, api (mcp http_auth imports app.api.routers.auth.tokens)
# monitoring: Prometheus/Sentry exporters used by the production metrics surface.
# graph: langgraph summarize pipeline + Postgres checkpointer — sole summarize path since T9, so required at runtime (pyproject opt-in).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=cache,target=/root/.cache/pip \
    echo "Starting uv sync with extras: ml youtube export scheduler mcp api browser_scraper attachment monitoring otel graph" && \
    uv sync --compile-bytecode --frozen --no-dev --extra ml --extra youtube --extra export --extra scheduler --extra mcp --extra api --extra browser_scraper --extra attachment --extra monitoring --extra otel --extra graph --verbose && \
    echo "uv sync completed successfully"

# =============================================================================
# Stage 2: Runtime - Minimal production image
# =============================================================================
FROM python:3.13-slim@sha256:2b7445fb71ca9cb15e9aab053fe8cb3162796f8e1d92ada12a49c766a811bc1e AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app:$PYTHONPATH"

WORKDIR /app

# Install only runtime dependencies (no build tools)
# ffmpeg is required for yt-dlp video/audio merging (can be disabled with build-arg)
ARG WITH_FFMPEG=1
RUN set -eux; \
    apt-get update; \
    pkgs="sqlite3 libsqlite3-0 libxml2 libxslt1.1 zlib1g git git-lfs"; \
    if [ "${WITH_FFMPEG}" = "1" ]; then pkgs="$pkgs ffmpeg"; fi; \
    apt-get install -y --no-install-recommends ${pkgs}; \
    rm -rf /var/lib/apt/lists/*; \
    apt-get clean

# Copy virtual environment from builder stage
COPY --from=builder /app/.venv /app/.venv

# Install Chromium for Playwright-based scraping fallback.
# Browsers are placed in /ms-playwright (shared, world-readable) so that the
# non-root `appuser` at runtime can find them — without PLAYWRIGHT_BROWSERS_PATH
# the install lands in /root/.cache/ms-playwright and appuser fails with
# "Executable doesn't exist at /home/appuser/.cache/ms-playwright/...".
# patchright is Scrapling's stealth Playwright backend; it honours the same
# env var and ships its own patched Chromium build.
# Set WITH_PLAYWRIGHT=0 at build time to produce a slimmer image without
# the browser engine (e.g. when SCRAPER_PLAYWRIGHT_ENABLED=false).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ARG WITH_PLAYWRIGHT=1
RUN set -eux; \
    if [ "${WITH_PLAYWRIGHT}" = "1" ]; then \
        mkdir -p /ms-playwright; \
        playwright install --with-deps chromium; \
        patchright install chromium; \
        chmod -R a+rX /ms-playwright; \
    fi

# Copy application code
COPY app ./app
COPY bot.py ./
COPY alembic.ini ./
# Operational config (non-secret tunables). Baked into the image so
# `load_ratatoskr_yaml` finds it at /app/config/ratatoskr.yaml. Secrets
# still flow through .env / process env per the precedence convention in
# app/config/_secret_marker.py.
COPY config ./config

# Extract the reviewed frontend artifact last. app/static/web is excluded from
# the build context, so stale local assets can neither leak into nor mutate the
# image. The archive includes .source-commit for provenance.
ADD ops/docker/ratatoskr-web.bundle.tar.gz ./app/static/web/

# Create the non-root runtime user and its writable data directory.  Keep the
# application and virtualenv root-owned/read-only: recursively changing /app
# would duplicate the multi-gigabyte virtualenv in a new image layer whenever
# application sources change.
RUN test -s /app/app/static/web/index.html \
    && test -s /app/app/static/web/.source-commit \
    && useradd -r -u 1000 -m -d /home/appuser -s /sbin/nologin appuser \
    && install -d -o appuser -g appuser /data

# Runtime configuration
VOLUME ["/data"]
ENV DB_PATH=/data/ratatoskr.db \
    LOG_LEVEL=INFO \
    REQUEST_TIMEOUT_SEC=60 \
    TEXTACY_ENABLED=1 \
    RATATOSKR_PROCESS_ROLE=bot

# Switch to non-root user
USER appuser

# Health check pings the runtime Database via the async healthcheck CLI,
# which respects DATABASE_URL (Postgres) without dragging in sqlite3 paths.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -m app.cli.healthcheck

CMD ["python", "-m", "bot"]
