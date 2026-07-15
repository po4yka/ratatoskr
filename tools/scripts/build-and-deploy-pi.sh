#!/usr/bin/env bash
# Build Ratatoskr Docker images locally (linux/arm64) and stream them to the
# Raspberry Pi over SSH so the Pi never has to perform the heavy build.
#
# Usage:
#   tools/scripts/build-and-deploy-pi.sh                                # build + ship + restart `ratatoskr`
#   tools/scripts/build-and-deploy-pi.sh --service mobile-api
#   tools/scripts/build-and-deploy-pi.sh --service ratatoskr --service worker --service scheduler
#   tools/scripts/build-and-deploy-pi.sh --services "ratatoskr worker scheduler"
#   tools/scripts/build-and-deploy-pi.sh --all                          # all supported services
#   tools/scripts/build-and-deploy-pi.sh --no-restart                   # just ship the image(s)
#   tools/scripts/build-and-deploy-pi.sh --no-cache                     # full rebuild
#   tools/scripts/build-and-deploy-pi.sh --migrate-only                 # render Alembic SQL dry-run on the Pi
#   tools/scripts/build-and-deploy-pi.sh --migrate-only --apply         # apply Alembic migrations on the Pi
#   tools/scripts/build-and-deploy-pi.sh --service ratatoskr --rollback # swap latest/previous and recreate
#
# Supported services: ratatoskr, worker, scheduler, mcp, mcp-write,
# mcp-public, mobile-api. Services sharing ops/docker/Dockerfile (everything
# except mobile-api) are built once and re-tagged for each requested service.
# Migration application is intentionally separate from deployment. Use
# `--migrate-only` for a dry-run and `--migrate-only --apply` to mutate the
# schema. Each Dockerfile group streams as a single tar so `docker load`
# deduplicates layers on the Pi.
#
# Environment overrides:
#   RASPI_HOST          SSH host alias                   (default: raspi)
#   RASPI_REMOTE_PATH   Repo path on the Pi              (default: ~/ratatoskr)
#   COMPOSE_PROJECT     Compose project name on the Pi   (default: docker)
#   COMPOSE_ENV_FILE    Env file passed to compose       (default: .env)
#   PI_HEALTH_TIMEOUT_SECONDS  Post-restart health wait   (default: 240)
#   PI_HEALTH_POLL_SECONDS     Health polling interval    (default: 5)
#   WITH_PLAYWRIGHT     mobile-api chromium install      (default: 0)
#                       — the Pi overlay sets SCRAPER_PLAYWRIGHT_ENABLED=false
#                       for mobile-api, so chromium is unused at runtime and
#                       carrying it bloats the image by ~4 GB. Override to 1
#                       only if you need the binaries.
#
# Compose tags built images as `<project>-<service>` (e.g. docker-ratatoskr).
# Default project is `docker` to match the running Pi stack (postgres/redis
# are started from inside ops/docker/, so their project name is the directory
# name). This script tags the local build with that exact name and pins the
# project on the Pi with `-p ${COMPOSE_PROJECT}`, so `compose up` reuses the
# shipped image instead of rebuilding it.
#
# The app-service restart uses `--no-deps --force-recreate ${SERVICE}` so we
# never disturb postgres/redis/qdrant during recreation. A post-recreate
# `docker network connect docker_default` works around a compose quirk that
# occasionally drops the default-network attachment for mobile-api under
# --no-deps.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"

RASPI_HOST=${RASPI_HOST:-raspi}
RASPI_REMOTE_PATH=${RASPI_REMOTE_PATH:-'~/ratatoskr'}
COMPOSE_PROJECT=${COMPOSE_PROJECT:-docker}
COMPOSE_ENV_FILE=${COMPOSE_ENV_FILE:-.env}
PLATFORM=linux/arm64
WITH_PLAYWRIGHT=${WITH_PLAYWRIGHT:-0}
PI_HEALTH_TIMEOUT_SECONDS=${PI_HEALTH_TIMEOUT_SECONDS:-240}
PI_HEALTH_POLL_SECONDS=${PI_HEALTH_POLL_SECONDS:-5}

for value_name in PI_HEALTH_TIMEOUT_SECONDS PI_HEALTH_POLL_SECONDS; do
  value=${!value_name}
  if [[ ! "$value" =~ ^[1-9][0-9]*$ ]]; then
    echo "${value_name} must be a positive integer (got '${value}')" >&2
    exit 2
  fi
done

SHARED_DOCKERFILE=ops/docker/Dockerfile
API_DOCKERFILE=ops/docker/Dockerfile.api
MIGRATE_SERVICE=migrate
SHARED_SERVICES=(ratatoskr worker scheduler mcp mcp-write mcp-public)
API_SERVICES=(mobile-api)
ALL_SERVICES=("${SHARED_SERVICES[@]}" "${API_SERVICES[@]}")

SERVICES=()
RESTART=1
NO_CACHE=0
ROLLBACK=0
MIGRATE_ONLY=0
APPLY_MIGRATIONS=0
GIT_SHA=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "unknown")
DEPLOYED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

usage() {
  sed -n '2,/^set -euo pipefail$/p' "$0" | sed '$d'
}

while [[ $# -gt 0 ]]; do
  case $1 in
    --service)
      [[ $# -ge 2 ]] || { echo "--service requires an argument" >&2; exit 2; }
      SERVICES+=("$2"); shift 2 ;;
    --service=*)
      SERVICES+=("${1#*=}"); shift ;;
    --services)
      [[ $# -ge 2 ]] || { echo "--services requires an argument" >&2; exit 2; }
      # shellcheck disable=SC2206  # intentional word-split on space-separated list
      SERVICES+=($2); shift 2 ;;
    --services=*)
      val=${1#*=}
      # shellcheck disable=SC2206
      SERVICES+=($val); shift ;;
    --all)
      SERVICES=("${ALL_SERVICES[@]}"); shift ;;
    --no-restart)
      RESTART=0; shift ;;
    --skip-migrate)
      echo "WARNING: --skip-migrate is deprecated; migrations are no longer run during deploy" >&2
      shift ;;
    --migrate-only)
      MIGRATE_ONLY=1; RESTART=0; shift ;;
    --apply)
      APPLY_MIGRATIONS=1; shift ;;
    --rollback)
      ROLLBACK=1; shift ;;
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

if [[ $ROLLBACK -eq 1 && $MIGRATE_ONLY -eq 1 ]]; then
  echo "--rollback cannot be combined with --migrate-only" >&2
  exit 2
fi

if [[ $APPLY_MIGRATIONS -eq 1 && $MIGRATE_ONLY -eq 0 ]]; then
  echo "--apply is only valid with --migrate-only" >&2
  exit 2
fi

# Default: build the bot only (backward-compat with the old single-service script).
[[ ${#SERVICES[@]} -eq 0 ]] && SERVICES=(ratatoskr)

if [[ $MIGRATE_ONLY -eq 1 ]]; then
  SERVICES=("$MIGRATE_SERVICE")
fi

# Validate and bucket each requested service by its Dockerfile.
SHARED_TO_BUILD=()
API_TO_BUILD=()
for svc in "${SERVICES[@]}"; do
  matched=0
  if [[ "$svc" == "$MIGRATE_SERVICE" ]]; then
    SHARED_TO_BUILD+=("$svc")
    continue
  fi
  for shared in "${SHARED_SERVICES[@]}"; do
    [[ "$svc" == "$shared" ]] && { SHARED_TO_BUILD+=("$svc"); matched=1; break; }
  done
  if [[ $matched -eq 0 ]]; then
    for api in "${API_SERVICES[@]}"; do
      [[ "$svc" == "$api" ]] && { API_TO_BUILD+=("$svc"); matched=1; break; }
    done
  fi
  if [[ $matched -eq 0 ]]; then
    echo "unsupported service: $svc (expected: ${ALL_SERVICES[*]})" >&2
    exit 2
  fi
done

if [[ $ROLLBACK -eq 1 ]]; then
  SHARED_TO_BUILD=()
  API_TO_BUILD=()
fi

command -v docker >/dev/null || { echo "docker is not on PATH" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is required" >&2; exit 1; }

echo "==> Verifying SSH to ${RASPI_HOST}"
REMOTE_ARCH=$(ssh -o BatchMode=yes "$RASPI_HOST" uname -m)
echo "    remote arch: $REMOTE_ARCH"
if [[ "$REMOTE_ARCH" != "aarch64" && "$REMOTE_ARCH" != "arm64" ]]; then
  echo "WARNING: remote arch '$REMOTE_ARCH' is not aarch64/arm64; the linux/arm64 image will not run there." >&2
fi

# build_and_ship <dockerfile> [KEY=VAL ...] -- <service> [service ...]
# Builds the dockerfile once, tags the resulting image as
# ${COMPOSE_PROJECT}-${svc}:latest for every trailing service, then streams
# all tags in one `docker save` invocation (which deduplicates layers).
build_and_ship() {
  local dockerfile=$1; shift
  local -a build_args=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do
    build_args+=(--build-arg "$1"); shift
  done
  shift  # consume the --
  local -a services=("$@")
  [[ ${#services[@]} -eq 0 ]] && return 0

  local primary="${services[0]}"
  local primary_tag="${COMPOSE_PROJECT}-${primary}:latest"
  local -a all_tags=()
  for s in "${services[@]}"; do
    all_tags+=("${COMPOSE_PROJECT}-${s}:latest")
  done

  echo "==> Building ${primary_tag} for ${PLATFORM} (dockerfile: ${dockerfile})"
  [[ ${#services[@]} -gt 1 ]] && echo "    will retag for: ${services[*]:1}"
  [[ ${#build_args[@]} -gt 0 ]] && echo "    build args: ${build_args[*]}"
  local -a build_flags=(--platform "$PLATFORM" -f "$dockerfile" -t "$primary_tag" --load)
  build_flags+=(--label "org.opencontainers.image.revision=${GIT_SHA}")
  build_flags+=(--label "org.opencontainers.image.created=${DEPLOYED_AT}")
  [[ $NO_CACHE -eq 1 ]] && build_flags+=(--no-cache)
  [[ ${#build_args[@]} -gt 0 ]] && build_flags+=("${build_args[@]}")
  DOCKER_BUILDKIT=1 docker buildx build "${build_flags[@]}" .

  # Re-tag the freshly-built image for each additional service.
  for s in "${services[@]:1}"; do
    docker tag "$primary_tag" "${COMPOSE_PROJECT}-${s}:latest"
  done

  echo "==> Streaming ${#all_tags[@]} tag(s) to ${RASPI_HOST}: ${all_tags[*]}"
  # `ssh 'gunzip | docker load'` occasionally exits 255 after the remote
  # docker load completes (SSH disconnects before flushing). Treat exit code
  # as advisory and verify by checking that each tag exists on the Pi.
  set +e
  docker save "${all_tags[@]}" | gzip | ssh "$RASPI_HOST" 'gunzip | docker load'
  local stream_exit=$?
  set -e
  [[ $stream_exit -ne 0 ]] && echo "    (ssh exited $stream_exit -- verifying tag presence on Pi)"

  # Verify each tag resolves to THIS build on the Pi. Mere presence is not
  # enough: while a multi-GB `docker load` is still settling (or when ssh exits
  # 255 before the load finalizes) the tag can briefly still point at the
  # PREVIOUS image, which then gets picked up by the recreate below (observed
  # 2026-07-14: mobile-api recreated on the old image and crash-looped its
  # schema-at-head gate). Cross-host image-ID comparison is unreliable (buildx
  # --load on Apple Silicon reports the manifest-list digest locally vs a
  # single-platform config digest on the Pi), so match the immutable
  # `org.opencontainers.image.created` label (unique per deploy) baked in at
  # build time instead.
  for s in "${services[@]}"; do
    local tag="${COMPOSE_PROJECT}-${s}:latest"
    local remote_created="" remote_id=""
    for attempt in 1 2 3 4 5 6 7 8; do
      remote_created=$(ssh -o BatchMode=yes "$RASPI_HOST" \
        "docker image inspect ${tag} --format '{{ index .Config.Labels \"org.opencontainers.image.created\" }}'" 2>/dev/null || true)
      [[ "$remote_created" == "$DEPLOYED_AT" ]] && break
      echo "    ${tag} not yet this build (created='${remote_created:-<none>}', want '${DEPLOYED_AT}'); retry ${attempt}/8 in 3s..." >&2
      sleep 3
    done
    if [[ "$remote_created" != "$DEPLOYED_AT" ]]; then
      echo "ERROR: ${tag} on Pi is not this build (created='${remote_created:-<none>}', want '${DEPLOYED_AT}') -- docker load may have failed or stalled" >&2
      exit 1
    fi
    remote_id=$(ssh -o BatchMode=yes "$RASPI_HOST" \
      "docker image inspect ${tag} --format '{{.Id}}'" 2>/dev/null || true)
    echo "    ${tag} -> ${remote_id} (created ${remote_created})"
  done
}

# Build each Dockerfile group at most once.
if [[ ${#SHARED_TO_BUILD[@]} -gt 0 ]]; then
  build_and_ship "$SHARED_DOCKERFILE" -- "${SHARED_TO_BUILD[@]}"
fi
if [[ ${#API_TO_BUILD[@]} -gt 0 ]]; then
  build_and_ship "$API_DOCKERFILE" "WITH_PLAYWRIGHT=${WITH_PLAYWRIGHT}" -- "${API_TO_BUILD[@]}"
fi

COMPOSE_RUN=(
  docker compose
  --env-file "${COMPOSE_ENV_FILE}"
  -p "${COMPOSE_PROJECT}"
  -f ops/docker/docker-compose.yml
  -f ops/docker/docker-compose.pi.yml
)

run_remote_migrations() {
  local -a migrate_args=()
  if [[ $APPLY_MIGRATIONS -eq 1 ]]; then
    echo "==> Applying database migrations on ${RASPI_HOST}"
    migrate_args+=(--apply)
  else
    echo "==> Rendering database migration SQL dry-run on ${RASPI_HOST}"
  fi
  # `compose run` has no `--no-build` flag before compose v2.27 (the Pi runs
  # v2.24.6) and does not build without an explicit `--build` anyway, so the
  # flag is both unsupported and redundant here -- the migrate image is streamed
  # in by build_and_ship above. `up` does support `--no-build`, so it stays.
  # Pass the full command explicitly. `docker compose run migrate --apply`
  # REPLACES the service's `command:` with `--apply`, so tini tries to exec
  # `--apply` as a program ("exec --apply failed: No such file or directory").
  # Spelling out `python -m app.cli.migrate_db` keeps the entrypoint intact and
  # appends the flag as an argument.
  ssh "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && \
    ${COMPOSE_RUN[*]} up -d --no-build postgres && \
    ( ${COMPOSE_RUN[*]} rm -sf ${MIGRATE_SERVICE} >/dev/null 2>&1 || true ) && \
    ${COMPOSE_RUN[*]} run --rm ${MIGRATE_SERVICE} python -m app.cli.migrate_db ${migrate_args[*]}"
}

tag_running_image_as_previous() {
  local svc=$1
  local previous_tag="${COMPOSE_PROJECT}-${svc}:previous"
  echo "==> Tagging currently running ${svc} image as ${previous_tag}"
  ssh "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && \
    CID=\$(${COMPOSE_RUN[*]} ps -q ${svc} 2>/dev/null || true); \
    if [ -n \"\$CID\" ]; then \
      IMG=\$(docker inspect --format '{{.Image}}' \"\$CID\"); \
      docker tag \"\$IMG\" '${previous_tag}'; \
      echo \"    ${previous_tag} -> \$IMG\"; \
    else \
      echo '    no running container; previous tag unchanged'; \
    fi"
}

restart_service_verified() {
  # `compose up --force-recreate` can land on a STALE image if the :latest tag
  # was not yet settled when it ran (see the build_and_ship note). That verify
  # already blocks until the tag is this build, but confirm here too and retry:
  # after recreate the running container's image must equal the current :latest.
  # Same-host image-ID comparison is reliable (unlike the cross-host case), so
  # compare the container's .Image to the :latest .Id directly.
  local svc=$1
  local latest_tag="${COMPOSE_PROJECT}-${svc}:latest"
  local attempt verdict
  for attempt in 1 2 3; do
    # `|| true`: a transient ssh drop during recreate must not abort under
    # `set -e`; the verify below (NO_CONTAINER / MISMATCH) drives the retry.
    ssh "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && ${COMPOSE_RUN[*]} up -d --no-deps --force-recreate ${svc}" || true
    verdict=$(ssh -o BatchMode=yes "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && \
      CID=\$(${COMPOSE_RUN[*]} ps -q ${svc} 2>/dev/null); \
      [ -n \"\$CID\" ] || { echo NO_CONTAINER; exit 0; }; \
      RUN_IMG=\$(docker inspect --format '{{.Image}}' \"\$CID\" 2>/dev/null); \
      LATEST_IMG=\$(docker image inspect '${latest_tag}' --format '{{.Id}}' 2>/dev/null); \
      if [ -n \"\$RUN_IMG\" ] && [ \"\$RUN_IMG\" = \"\$LATEST_IMG\" ]; then echo MATCH; \
      else echo \"MISMATCH run=\${RUN_IMG:-none} latest=\${LATEST_IMG:-none}\"; fi" 2>/dev/null || true)
    if [[ "$verdict" == MATCH ]]; then
      echo "    ${svc} confirmed running on current ${latest_tag}"
      return 0
    fi
    echo "    ${svc} recreate landed on stale/no image (${verdict}); retry ${attempt}/3 in 4s..." >&2
    sleep 4
  done
  echo "ERROR: ${svc} did not come up on ${latest_tag} after 3 recreate attempts" >&2
  exit 1
}

diagnose_service_health() {
  local svc=$1
  echo "==> Diagnostic state for ${svc}" >&2
  ssh -o BatchMode=yes "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && \
    CID=\$(${COMPOSE_RUN[*]} ps -q ${svc} 2>/dev/null || true); \
    if [ -z \"\$CID\" ]; then \
      echo '    container not found' >&2; \
    else \
      docker inspect --format '{{json .State}}' \"\$CID\" 2>/dev/null || true; \
      ${COMPOSE_RUN[*]} logs --no-color --tail=50 ${svc} 2>/dev/null || true; \
    fi" >&2 || true
}

wait_for_service_health() {
  local svc=$1
  local deadline=$((SECONDS + PI_HEALTH_TIMEOUT_SECONDS))
  local verdict=""

  echo "==> Waiting up to ${PI_HEALTH_TIMEOUT_SECONDS}s for ${svc} health"
  while (( SECONDS < deadline )); do
    verdict=$(ssh -o BatchMode=yes "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && \
      CID=\$(${COMPOSE_RUN[*]} ps -q ${svc} 2>/dev/null || true); \
      [ -n \"\$CID\" ] || { echo 'missing none'; exit 0; }; \
      STATE=\$(docker inspect --format '{{.State.Status}}' \"\$CID\" 2>/dev/null || true); \
      HEALTH=\$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \"\$CID\" 2>/dev/null || true); \
      echo \"\${STATE:-unknown} \${HEALTH:-unknown}\"" 2>/dev/null || true)

    case "$verdict" in
      "running healthy")
        echo "    ${svc} is healthy"
        return 0
        ;;
      "running starting"|"unknown unknown"|"")
        ;;
      "running unhealthy")
        echo "ERROR: ${svc} reported unhealthy after restart" >&2
        diagnose_service_health "$svc"
        return 1
        ;;
      "running none")
        echo "ERROR: ${svc} has no Docker healthcheck" >&2
        diagnose_service_health "$svc"
        return 1
        ;;
      *)
        echo "ERROR: ${svc} stopped while waiting for health (state: ${verdict})" >&2
        diagnose_service_health "$svc"
        return 1
        ;;
    esac

    sleep "$PI_HEALTH_POLL_SECONDS"
  done

  echo "ERROR: timed out after ${PI_HEALTH_TIMEOUT_SECONDS}s waiting for ${svc} health (last state: ${verdict:-unknown})" >&2
  diagnose_service_health "$svc"
  return 1
}

rollback_service_image() {
  local svc=$1
  local latest_tag="${COMPOSE_PROJECT}-${svc}:latest"
  local previous_tag="${COMPOSE_PROJECT}-${svc}:previous"
  local tmp_tag="${COMPOSE_PROJECT}-${svc}:rollback-tmp"
  echo "==> Rolling back ${svc}: swapping ${latest_tag} and ${previous_tag}"
  ssh "$RASPI_HOST" "set -e; \
    docker image inspect '${previous_tag}' >/dev/null; \
    docker image inspect '${latest_tag}' >/dev/null; \
    LATEST_ID=\$(docker image inspect '${latest_tag}' --format '{{.Id}}'); \
    PREVIOUS_ID=\$(docker image inspect '${previous_tag}' --format '{{.Id}}'); \
    docker tag \"\$LATEST_ID\" '${tmp_tag}'; \
    docker tag \"\$PREVIOUS_ID\" '${latest_tag}'; \
    docker tag '${tmp_tag}' '${previous_tag}'; \
    docker rmi '${tmp_tag}' >/dev/null 2>&1 || true"
}

remote_image_label() {
  local tag=$1
  local label=$2
  ssh "$RASPI_HOST" "docker image inspect '${tag}' --format '{{ index .Config.Labels \"${label}\" }}' 2>/dev/null || true"
}

metric_label_value() {
  local value=$1
  value=${value//$'\n'/}
  value=${value//$'\r'/}
  if [[ -z "$value" || "$value" == "<no value>" ]]; then
    value=unknown
  fi
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf "%s" "$value"
}

write_deploy_metrics() {
  local svc=$1
  local latest_tag="${COMPOSE_PROJECT}-${svc}:latest"
  local previous_tag="${COMPOSE_PROJECT}-${svc}:previous"
  local current_sha current_at previous_sha previous_at metrics
  current_sha=$(remote_image_label "$latest_tag" "org.opencontainers.image.revision")
  current_at=$(remote_image_label "$latest_tag" "org.opencontainers.image.created")
  previous_sha=$(remote_image_label "$previous_tag" "org.opencontainers.image.revision")
  previous_at=$(remote_image_label "$previous_tag" "org.opencontainers.image.created")
  current_sha=$(metric_label_value "$current_sha")
  current_at=$(metric_label_value "$current_at")
  previous_sha=$(metric_label_value "$previous_sha")
  previous_at=$(metric_label_value "$previous_at")
  metrics=$(cat <<EOF
# HELP ratatoskr_deploy_version_info Ratatoskr deployed image version labels.
# TYPE ratatoskr_deploy_version_info gauge
ratatoskr_deploy_version_info{service="${svc}",slot="current",git_sha="${current_sha}",deployed_at="${current_at}"} 1
ratatoskr_deploy_version_info{service="${svc}",slot="previous",git_sha="${previous_sha}",deployed_at="${previous_at}"} 1
EOF
)
  echo "==> Writing deploy version metric for ${svc}"
  printf "%s\n" "$metrics" | ssh "$RASPI_HOST" "docker run --rm -i --user 0 -v ${COMPOSE_PROJECT}_pg_backup_metrics:/textfile '${latest_tag}' sh -c 'cat > /textfile/ratatoskr_deploy_${svc}.prom'"
}

if [[ $MIGRATE_ONLY -eq 1 ]]; then
  run_remote_migrations
elif [[ $RESTART -eq 1 ]]; then
  for svc in "${SERVICES[@]}"; do
    if [[ $ROLLBACK -eq 1 ]]; then
      rollback_service_image "$svc"
    else
      tag_running_image_as_previous "$svc"
    fi
    echo "==> Restarting ${svc} on ${RASPI_HOST} (project: ${COMPOSE_PROJECT})"
    restart_service_verified "$svc"

    # Workaround for a `compose up --no-deps --force-recreate` quirk observed
    # 2026-05-24: mobile-api ended up attached to only the external
    # `firecrawl_internal` network, with `docker_default` dropped. Bot didn't
    # reproduce. `docker network connect` errors with "already exists" when
    # correctly attached, so `|| true` keeps this idempotent across services
    # that may or may not need docker_default.
    echo "==> Ensuring ${svc} is attached to docker_default"
    ssh "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && \
      CID=\$(${COMPOSE_RUN[*]} ps -q ${svc} 2>/dev/null) && \
      [ -n \"\$CID\" ] && \
      docker network connect docker_default \"\$CID\" 2>/dev/null \
      && echo '    attached docker_default' \
      || echo '    docker_default already attached or not declared'"
    wait_for_service_health "$svc"
    write_deploy_metrics "$svc"
  done
else
  echo "==> Skipping restart (--no-restart). To start manually on the Pi:"
  for svc in "${SERVICES[@]}"; do
    echo "    ssh ${RASPI_HOST} 'cd ${RASPI_REMOTE_PATH} && ${COMPOSE_RUN[*]} up -d --no-deps --force-recreate ${svc}'"
  done
fi

echo "==> Done."
