#!/usr/bin/env bash
# Build the Ratatoskr Docker image locally (linux/arm64) and stream it to the
# Raspberry Pi over SSH so the Pi never has to perform the heavy build.
#
# Usage:
#   tools/scripts/build-and-deploy-pi.sh                    # build + ship + restart `ratatoskr`
#   tools/scripts/build-and-deploy-pi.sh --service mobile-api
#   tools/scripts/build-and-deploy-pi.sh --no-restart       # just ship the image
#   tools/scripts/build-and-deploy-pi.sh --no-cache         # full rebuild
#
# Supported services: ratatoskr, mobile-api, mcp, mcp-write, mcp-public.
#
# Environment overrides:
#   RASPI_HOST          SSH host alias                   (default: raspi)
#   RASPI_REMOTE_PATH   Repo path on the Pi              (default: ~/ratatoskr)
#   COMPOSE_PROJECT     Compose project name on the Pi   (default: docker)
#   COMPOSE_ENV_FILE    Env file passed to compose       (default: .env)
#
# Compose tags built images as `<project>-<service>` (e.g. docker-ratatoskr).
# Default project is `docker` to match the running Pi stack (postgres/redis are
# started from inside ops/docker/, so their project name is the directory name).
# This script tags the local build with that exact name and pins the project on
# the Pi with `-p ${COMPOSE_PROJECT}`, so `compose up` reuses the shipped image
# instead of rebuilding it.
#
# The restart uses `--no-deps --force-recreate ${SERVICE}` so we never disturb
# postgres/redis/qdrant which are managed independently.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"

RASPI_HOST=${RASPI_HOST:-raspi}
RASPI_REMOTE_PATH=${RASPI_REMOTE_PATH:-'~/ratatoskr'}
COMPOSE_PROJECT=${COMPOSE_PROJECT:-docker}
COMPOSE_ENV_FILE=${COMPOSE_ENV_FILE:-.env}
PLATFORM=linux/arm64

SERVICE=ratatoskr
RESTART=1
NO_CACHE=0

usage() {
  sed -n '2,18p' "$0"
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --service)
      [[ $# -ge 2 ]] || { echo "--service requires an argument" >&2; exit 2; }
      SERVICE=$2; shift 2 ;;
    --service=*)
      SERVICE=${1#*=}; shift ;;
    --no-restart)
      RESTART=0; shift ;;
    --no-cache)
      NO_CACHE=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2 ;;
  esac
done

case "$SERVICE" in
  ratatoskr|mcp|mcp-write|mcp-public)
    DOCKERFILE=ops/docker/Dockerfile ;;
  mobile-api)
    DOCKERFILE=ops/docker/Dockerfile.api ;;
  *)
    echo "unsupported service: $SERVICE (expected: ratatoskr, mobile-api, mcp, mcp-write, mcp-public)" >&2
    exit 2 ;;
esac

IMAGE_TAG="${COMPOSE_PROJECT}-${SERVICE}:latest"

command -v docker >/dev/null || { echo "docker is not on PATH" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is required" >&2; exit 1; }

echo "==> Verifying SSH to ${RASPI_HOST}"
REMOTE_ARCH=$(ssh -o BatchMode=yes "$RASPI_HOST" uname -m)
echo "    remote arch: $REMOTE_ARCH"
if [[ "$REMOTE_ARCH" != "aarch64" && "$REMOTE_ARCH" != "arm64" ]]; then
  echo "WARNING: remote arch '$REMOTE_ARCH' is not aarch64/arm64; the linux/arm64 image will not run there." >&2
fi

echo "==> Building ${IMAGE_TAG} for ${PLATFORM} (dockerfile: ${DOCKERFILE})"
BUILD_FLAGS=(--platform "$PLATFORM" -f "$DOCKERFILE" -t "$IMAGE_TAG" --load)
if [[ $NO_CACHE -eq 1 ]]; then
  BUILD_FLAGS+=(--no-cache)
fi
DOCKER_BUILDKIT=1 docker buildx build "${BUILD_FLAGS[@]}" .

LOCAL_SHA=$(docker image inspect "$IMAGE_TAG" --format '{{.Id}}')
echo "==> Streaming ${IMAGE_TAG} to ${RASPI_HOST} (gzip in transit)"
echo "    local image SHA: $LOCAL_SHA"

# Read the streamed image's SHA back from the Pi, retrying on transient
# failures. The verifying `ssh ... docker image inspect` call occasionally
# exits 255 (connection dropped) even though the image loaded fine, and a
# slow `docker load` may not have registered the image by the first probe.
# Retry a few times before treating an empty result as a real failure --
# otherwise a successful deploy aborts before the restart step.
remote_image_sha() {
  local attempt sha="" max_attempts=5
  for ((attempt = 1; attempt <= max_attempts; attempt++)); do
    sha=$(ssh -o BatchMode=yes "$RASPI_HOST" \
      "docker image inspect ${IMAGE_TAG} --format '{{.Id}}'" 2>/dev/null || true)
    if [[ -n "$sha" ]]; then
      printf '%s\n' "$sha"
      return 0
    fi
    if [[ $attempt -lt $max_attempts ]]; then
      echo "    image SHA probe ${attempt}/${max_attempts} empty; retrying in 3s..." >&2
      sleep 3
    fi
  done
  return 1
}

# `ssh 'gunzip | docker load'` occasionally exits 255 after the remote `docker
# load` completes (SSH disconnects before flushing the channel). Treat exit
# code as advisory: verify success by inspecting the image SHA on the Pi.
set +e
docker save "$IMAGE_TAG" | gzip | ssh "$RASPI_HOST" 'gunzip | docker load'
STREAM_EXIT=$?
set -e
REMOTE_SHA=$(remote_image_sha || true)
if [[ -z "$REMOTE_SHA" || "$LOCAL_SHA" != "$REMOTE_SHA" ]]; then
  echo "ERROR: image stream verification failed" >&2
  echo "  local SHA:  $LOCAL_SHA" >&2
  echo "  remote SHA: ${REMOTE_SHA:-<not found>}" >&2
  echo "  ssh exit:   $STREAM_EXIT" >&2
  echo "  hint: the image may have loaded despite this -- verify with:" >&2
  echo "    ssh ${RASPI_HOST} \"docker image inspect ${IMAGE_TAG} --format '{{.Id}}'\"" >&2
  exit 1
fi
if [[ $STREAM_EXIT -ne 0 ]]; then
  echo "    (ssh exited $STREAM_EXIT but image SHA matches — proceeding)"
fi

COMPOSE_RUN=(
  docker compose
  --env-file "${COMPOSE_ENV_FILE}"
  -p "${COMPOSE_PROJECT}"
  -f ops/docker/docker-compose.yml
  -f ops/docker/docker-compose.pi.yml
)
COMPOSE_CMD="${COMPOSE_RUN[*]} up -d --no-deps --force-recreate ${SERVICE}"

if [[ $RESTART -eq 1 ]]; then
  echo "==> Restarting ${SERVICE} on ${RASPI_HOST} (project: ${COMPOSE_PROJECT})"
  ssh "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && ${COMPOSE_CMD}"

  # Workaround for a `compose up --no-deps --force-recreate` quirk observed
  # 2026-05-24: `ratatoskr-mobile-api` ended up attached to only the external
  # `firecrawl_internal` network, with `docker_default` (where postgres/redis
  # live) dropped. Bot/worker/scheduler didn't reproduce, but the mobile-api
  # crash-loop was silent — alembic plugins logged, then TCP connect to
  # postgres hung until the container died with exit 1 at ~17 s, no traceback.
  # `docker network connect` errors with "already exists" when the container
  # is correctly attached, so the `|| true` keeps the script idempotent across
  # services that may or may not need docker_default.
  echo "==> Ensuring ${SERVICE} is attached to docker_default"
  ssh "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && \
    CID=\$(${COMPOSE_RUN[*]} ps -q ${SERVICE} 2>/dev/null) && \
    [ -n \"\$CID\" ] && \
    docker network connect docker_default \"\$CID\" 2>/dev/null \
    && echo '    attached docker_default' \
    || echo '    docker_default already attached or not declared'"
else
  echo "==> Skipping restart (--no-restart). To start manually on the Pi:"
  echo "    ssh ${RASPI_HOST} 'cd ${RASPI_REMOTE_PATH} && ${COMPOSE_CMD}'"
fi

echo "==> Done."
