#!/usr/bin/env bash
# Build Ratatoskr Docker images locally (linux/arm64) and stream them to the
# Raspberry Pi over SSH so the Pi never has to perform the heavy build.
#
# Usage:
#   tools/scripts/build-and-deploy-pi.sh                                # deploy the compatible reader stack
#   tools/scripts/build-and-deploy-pi.sh --services "ratatoskr worker scheduler mobile-api"
#   tools/scripts/build-and-deploy-pi.sh --service pg-backup
#   tools/scripts/build-and-deploy-pi.sh --all                          # all supported services
#   tools/scripts/build-and-deploy-pi.sh --no-restart                   # just ship the image(s)
#   tools/scripts/build-and-deploy-pi.sh --no-cache                     # full rebuild
#   tools/scripts/build-and-deploy-pi.sh --services "..." --resolve-services # print dependency order
#   tools/scripts/build-and-deploy-pi.sh --migrate-only                 # render Alembic SQL dry-run on the Pi
#   tools/scripts/build-and-deploy-pi.sh --migrate-only --apply         # apply Alembic migrations on the Pi
#   tools/scripts/build-and-deploy-pi.sh --rollback                      # roll back the compatible reader stack
#
# Supported services include the app processes, pg-backup, and the two isolated
# AI re-auth display/browser pairs. App and display services sharing a
# Dockerfile are built once and re-tagged; the pinned CloakBrowser image is
# pulled and verified rather than rebuilt.
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
#   WITH_PLAYWRIGHT     mobile-api chromium install      (default: 1)
#                       Keep this aligned with the Pi overlay's enabled
#                       Playwright, Crawlee, and Scrapling browser fallbacks.
#                       Set to 0 only together with disabling those providers.
#
# Compose tags built images as `<project>-<service>` (e.g. docker-ratatoskr).
# Default project is `docker` to match the running Pi stack (postgres/redis
# are started from inside ops/docker/, so their project name is the directory
# name). This script tags the local build with that exact name and pins the
# project on the Pi with `-p ${COMPOSE_PROJECT}`, so `compose up` reuses the
# shipped image instead of rebuilding it.
#
# The app-service restart uses `--no-deps --force-recreate ${SERVICE}` so it
# never disturbs active data services. A Pi-only preflight retires the obsolete
# Compose Qdrant container only after the host-native qdrant.service passes
# readiness; its named volume is preserved. A post-recreate network reconnect
# works around a compose quirk that occasionally drops the default-network
# attachment under --no-deps while preserving Compose service-name discovery.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
cd "$REPO_ROOT"

RASPI_HOST=${RASPI_HOST:-raspi}
RASPI_REMOTE_PATH=${RASPI_REMOTE_PATH:-'~/ratatoskr'}
COMPOSE_PROJECT=${COMPOSE_PROJECT:-docker}
COMPOSE_ENV_FILE=${COMPOSE_ENV_FILE:-.env}
PLATFORM=linux/arm64
WITH_PLAYWRIGHT=${WITH_PLAYWRIGHT:-1}
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
BACKUP_DOCKERFILE=ops/docker/pg-backup/Dockerfile
DISPLAY_DOCKERFILE=ops/docker/ai-backup-display/Dockerfile
WEBAUTHN_DOCKERFILE=ops/docker/ai-backup-webauthn-bridge/Dockerfile
PINNED_CLOAKBROWSER_IMAGE=cloakhq/cloakbrowser@sha256:a333b754fe9da1fd16851f2bb69f258601d4c7fa36e8b26c15f1e031241076c1
MIGRATE_SERVICE=migrate
SHARED_SERVICES=(ratatoskr worker scheduler mcp mcp-write mcp-public)
API_SERVICES=(mobile-api)
BACKUP_SERVICES=(pg-backup)
DISPLAY_SERVICES=(ai-backup-display-chatgpt ai-backup-display-claude)
WEBAUTHN_SERVICES=(ai-backup-webauthn-bridge-chatgpt ai-backup-webauthn-bridge-claude)
BROWSER_SERVICES=(cloakbrowser-reauth-chatgpt cloakbrowser-reauth-claude)
MOBILE_API_CONTROL_NETWORKS=(ai_backup_control_chatgpt ai_backup_control_claude)
ALL_SERVICES=("${DISPLAY_SERVICES[@]}" "${WEBAUTHN_SERVICES[@]}" "${BROWSER_SERVICES[@]}" "${SHARED_SERVICES[@]}" "${API_SERVICES[@]}" "${BACKUP_SERVICES[@]}")
READER_RELEASE_SERVICES=(ratatoskr worker scheduler mobile-api)

SERVICES=()
RESTART=1
NO_CACHE=0
ROLLBACK=0
MIGRATE_ONLY=0
APPLY_MIGRATIONS=0
RESOLVE_SERVICES=0
GIT_SHA=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
FRONTEND_SHA=$(tr -d '[:space:]' < ops/docker/ratatoskr-web.commit)
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
    --resolve-services)
      RESOLVE_SERVICES=1; shift ;;
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

if [[ $ROLLBACK -eq 0 && $RESOLVE_SERVICES -eq 0 ]] && [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "ERROR: refusing to build from a dirty worktree because APP_BUILD would not identify its contents" >&2
  echo "       commit or remove all staged, unstaged, and untracked changes first" >&2
  exit 2
fi

# Default: deploy the compatibility group atomically. These processes share
# the source-artifact schema/capability and must never run mixed revisions.
[[ ${#SERVICES[@]} -eq 0 ]] && SERVICES=("${READER_RELEASE_SERVICES[@]}")

if [[ $MIGRATE_ONLY -eq 1 ]]; then
  SERVICES=("$MIGRATE_SERVICE")
fi

service_requested() {
  local expected=$1 svc
  for svc in "${SERVICES[@]}"; do
    [[ "$svc" == "$expected" ]] && return 0
  done
  return 1
}

add_reauth_prerequisites() {
  local provider browser display bridge
  local -a prerequisites=()
  for provider in chatgpt claude; do
    browser="cloakbrowser-reauth-${provider}"
    display="ai-backup-display-${provider}"
    bridge="ai-backup-webauthn-bridge-${provider}"
    service_requested "$browser" || continue
    service_requested "$display" || prerequisites+=("$display")
    service_requested "$bridge" || prerequisites+=("$bridge")
  done
  SERVICES=("${prerequisites[@]}" "${SERVICES[@]}")
}

order_reauth_services() {
  local candidate svc
  local -a ordered=()
  for candidate in "${DISPLAY_SERVICES[@]}" "${WEBAUTHN_SERVICES[@]}" "${BROWSER_SERVICES[@]}"; do
    service_requested "$candidate" && ordered+=("$candidate")
  done
  for svc in "${SERVICES[@]}"; do
    case "$svc" in
      ai-backup-display-chatgpt|ai-backup-display-claude|ai-backup-webauthn-bridge-chatgpt|ai-backup-webauthn-bridge-claude|cloakbrowser-reauth-chatgpt|cloakbrowser-reauth-claude) ;;
      *) ordered+=("$svc") ;;
    esac
  done
  SERVICES=("${ordered[@]}")
}

if [[ $MIGRATE_ONLY -eq 0 ]]; then
  add_reauth_prerequisites
  order_reauth_services
fi

# Validate and bucket each requested service by its Dockerfile.
SHARED_TO_BUILD=()
API_TO_BUILD=()
BACKUP_TO_BUILD=()
DISPLAY_TO_BUILD=()
WEBAUTHN_TO_BUILD=()
BROWSER_TO_START=()
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
    for backup in "${BACKUP_SERVICES[@]}"; do
      [[ "$svc" == "$backup" ]] && { BACKUP_TO_BUILD+=("$svc"); matched=1; break; }
    done
  fi
  if [[ $matched -eq 0 ]]; then
    for display in "${DISPLAY_SERVICES[@]}"; do
      [[ "$svc" == "$display" ]] && { DISPLAY_TO_BUILD+=("$svc"); matched=1; break; }
    done
  fi
  if [[ $matched -eq 0 ]]; then
    for webauthn in "${WEBAUTHN_SERVICES[@]}"; do
      [[ "$svc" == "$webauthn" ]] && { WEBAUTHN_TO_BUILD+=("$svc"); matched=1; break; }
    done
  fi
  if [[ $matched -eq 0 ]]; then
    for browser in "${BROWSER_SERVICES[@]}"; do
      [[ "$svc" == "$browser" ]] && { BROWSER_TO_START+=("$svc"); matched=1; break; }
    done
  fi
  if [[ $matched -eq 0 ]]; then
    echo "unsupported service: $svc (expected: ${ALL_SERVICES[*]})" >&2
    exit 2
  fi
done

if [[ $ROLLBACK -eq 1 ]]; then
  for svc in "${SERVICES[@]}"; do
    case "$svc" in
      cloakbrowser-reauth-chatgpt|cloakbrowser-reauth-claude)
        echo "ERROR: rollback is not supported for digest-pinned CloakBrowser services" >&2
        exit 2
        ;;
    esac
  done
  SHARED_TO_BUILD=()
  API_TO_BUILD=()
  BACKUP_TO_BUILD=()
  DISPLAY_TO_BUILD=()
  WEBAUTHN_TO_BUILD=()
  BROWSER_TO_START=()
fi

release_group_requested=0
if [[ $MIGRATE_ONLY -eq 0 ]]; then
  missing_release_services=()
  for required in "${READER_RELEASE_SERVICES[@]}"; do
    present=0
    for requested in "${SERVICES[@]}"; do
      [[ "$requested" == "$required" ]] && present=1
    done
    if [[ $present -eq 1 ]]; then
      release_group_requested=1
    else
      missing_release_services+=("$required")
    fi
  done
  if [[ $release_group_requested -eq 1 && ${#missing_release_services[@]} -gt 0 ]]; then
    echo "ERROR: reader-compatible services must deploy together; missing: ${missing_release_services[*]}" >&2
    echo "       use --services \"${READER_RELEASE_SERVICES[*]}\"" >&2
    exit 2
  fi
fi

if [[ $RESOLVE_SERVICES -eq 1 ]]; then
  printf '%s\n' "${SERVICES[@]}"
  exit 0
fi

command -v docker >/dev/null || { echo "docker is not on PATH" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is required" >&2; exit 1; }

echo "==> Verifying SSH to ${RASPI_HOST}"
REMOTE_ARCH=$(ssh -o BatchMode=yes "$RASPI_HOST" uname -m)
echo "    remote arch: $REMOTE_ARCH"
if [[ "$REMOTE_ARCH" != "aarch64" && "$REMOTE_ARCH" != "arm64" ]]; then
  echo "WARNING: remote arch '$REMOTE_ARCH' is not aarch64/arm64; the linux/arm64 image will not run there." >&2
fi

verify_remote_checkout() {
  local remote_checkout remote_sha remote_provenance
  remote_checkout=$(ssh -o BatchMode=yes "$RASPI_HOST" \
    "cd ${RASPI_REMOTE_PATH} && \
      SHA=\$(git rev-parse HEAD) && \
      if git diff --quiet -- ops/docker/cloakbrowser-reauth/entrypoint.sh ops/docker/ai-backup-webauthn-bridge ops/docker/docker-compose.yml && \
         git diff --cached --quiet -- ops/docker/cloakbrowser-reauth/entrypoint.sh ops/docker/ai-backup-webauthn-bridge ops/docker/docker-compose.yml; then \
        printf '%s clean\\n' \"\$SHA\"; \
      else \
        printf '%s dirty\\n' \"\$SHA\"; \
      fi")
  read -r remote_sha remote_provenance <<<"$remote_checkout"
  if [[ "$remote_sha" != "$GIT_SHA" ]]; then
    echo "ERROR: ${RASPI_HOST}:${RASPI_REMOTE_PATH} is at ${remote_sha:-<unknown>}, expected ${GIT_SHA}" >&2
    echo "       update the clean remote checkout to the exact pushed commit before deploying" >&2
    exit 1
  fi
  if [[ "$remote_provenance" != clean ]]; then
    echo "ERROR: ${RASPI_HOST}:${RASPI_REMOTE_PATH} has local changes to the re-auth compose/wrapper" >&2
    echo "       restore or commit those tracked files before deploying their bind-mounted contents" >&2
    exit 1
  fi
  ssh -o BatchMode=yes "$RASPI_HOST" \
    "cd ${RASPI_REMOTE_PATH} && \
      test -r ops/docker/cloakbrowser-reauth/entrypoint.sh && \
      test -r ops/docker/ai-backup-webauthn-bridge/Dockerfile && \
      test -r ops/docker/ai-backup-webauthn-bridge/entrypoint.sh && \
      test -r ops/docker/ai-backup-webauthn-bridge/healthcheck.sh"
  echo "    remote checkout: ${remote_sha}"
}

verify_remote_checkout

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
  build_and_ship "$SHARED_DOCKERFILE" "APP_BUILD=${GIT_SHA}" -- "${SHARED_TO_BUILD[@]}"
fi
if [[ ${#API_TO_BUILD[@]} -gt 0 ]]; then
  build_and_ship "$API_DOCKERFILE" "APP_BUILD=${GIT_SHA}" "WITH_PLAYWRIGHT=${WITH_PLAYWRIGHT}" -- "${API_TO_BUILD[@]}"
fi
if [[ ${#BACKUP_TO_BUILD[@]} -gt 0 ]]; then
  build_and_ship "$BACKUP_DOCKERFILE" -- "${BACKUP_TO_BUILD[@]}"
fi
if [[ ${#DISPLAY_TO_BUILD[@]} -gt 0 ]]; then
  build_and_ship "$DISPLAY_DOCKERFILE" -- "${DISPLAY_TO_BUILD[@]}"
fi
if [[ ${#WEBAUTHN_TO_BUILD[@]} -gt 0 ]]; then
  build_and_ship "$WEBAUTHN_DOCKERFILE" -- "${WEBAUTHN_TO_BUILD[@]}"
fi

COMPOSE_RUN=(
  docker compose
  --env-file "${COMPOSE_ENV_FILE}"
  --profile ai-backup-reauth
  -p "${COMPOSE_PROJECT}"
  -f ops/docker/docker-compose.yml
  -f ops/docker/docker-compose.pi.yml
)

is_isolated_reauth_service() {
  local svc=$1
  case "$svc" in
    ai-backup-display-chatgpt|ai-backup-display-claude|ai-backup-webauthn-bridge-chatgpt|ai-backup-webauthn-bridge-claude|cloakbrowser-reauth-chatgpt|cloakbrowser-reauth-claude)
      return 0 ;;
    *)
      return 1 ;;
  esac
}

is_pinned_browser_service() {
  local svc=$1
  [[ "$svc" == "cloakbrowser-reauth-chatgpt" || "$svc" == "cloakbrowser-reauth-claude" ]]
}

ensure_mobile_api_control_networks() {
  local svc=$1
  [[ "$svc" == "mobile-api" ]] || return 0

  local logical_network network
  for logical_network in "${MOBILE_API_CONTROL_NETWORKS[@]}"; do
    network="${COMPOSE_PROJECT}_${logical_network}"
    echo "==> Ensuring mobile-api is attached to ${network}"
    ssh "$RASPI_HOST" "set -eu; \
      cd ${RASPI_REMOTE_PATH}; \
      CID=\$(${COMPOSE_RUN[*]} ps -q mobile-api 2>/dev/null); \
      [ -n \"\$CID\" ]; \
      docker network inspect '${network}' >/dev/null; \
      if docker inspect --format '{{json .NetworkSettings.Networks}}' \"\$CID\" | grep -Fq '\"${network}\"'; then \
        echo '    already attached'; \
      else \
        docker network connect --alias 'mobile-api' '${network}' \"\$CID\"; \
        echo '    attached'; \
      fi; \
      docker inspect --format '{{json .NetworkSettings.Networks}}' \"\$CID\" | grep -Fq '\"${network}\"'"
  done
}

ensure_reauth_browser_networks() {
  local svc=$1
  local provider
  case "$svc" in
    cloakbrowser-reauth-chatgpt) provider=chatgpt ;;
    cloakbrowser-reauth-claude) provider=claude ;;
    *) return 0 ;;
  esac

  local network
  for network in \
    "${COMPOSE_PROJECT}_ai_backup_control_${provider}" \
    "${COMPOSE_PROJECT}_ai_backup_browser_egress_${provider}"; do
    echo "==> Ensuring ${svc} is attached to ${network}"
    ssh "$RASPI_HOST" "set -eu; \
      CID=\$(docker inspect --format '{{.Id}}' 'ratatoskr-${svc}'); \
      docker network inspect '${network}' >/dev/null; \
      if docker inspect --format '{{json .NetworkSettings.Networks}}' \"\$CID\" | grep -Fq '\"${network}\"'; then \
        echo '    already attached'; \
      else \
        docker network connect --alias '${svc}' '${network}' \"\$CID\"; \
        echo '    attached'; \
      fi; \
      docker inspect --format '{{json .NetworkSettings.Networks}}' \"\$CID\" | grep -Fq '\"${network}\"'"
  done
}

verify_pinned_cloakbrowser_image() {
  [[ ${#BROWSER_TO_START[@]} -gt 0 ]] || return 0
  echo "==> Verifying exact pinned CloakBrowser image on ${RASPI_HOST}"
  ssh "$RASPI_HOST" "set -eu; \
    cd ${RASPI_REMOTE_PATH}; \
    IMAGES=\$(${COMPOSE_RUN[*]} config --images); \
    printf '%s\\n' \"\$IMAGES\" | grep -Fqx '${PINNED_CLOAKBROWSER_IMAGE}' || { \
      echo 'ERROR: compose CLOAKBROWSER_IMAGE does not match the deployment pin' >&2; exit 1; }; \
    docker pull --platform linux/arm64 '${PINNED_CLOAKBROWSER_IMAGE}' >/dev/null; \
    docker image inspect '${PINNED_CLOAKBROWSER_IMAGE}' >/dev/null"
  echo "    ${PINNED_CLOAKBROWSER_IMAGE}"
}

verify_webauthn_host() {
  local requested=0 svc
  for svc in "${SERVICES[@]}"; do
    case "$svc" in
      ai-backup-webauthn-bridge-chatgpt|ai-backup-webauthn-bridge-claude|cloakbrowser-reauth-chatgpt|cloakbrowser-reauth-claude)
        requested=1
        break
        ;;
    esac
  done
  [[ $requested -eq 1 ]] || return 0

  echo "==> Verifying Bluetooth/WebAuthn host prerequisites on ${RASPI_HOST}"
  ssh "$RASPI_HOST" "set -eu; \
    test -S /run/dbus/system_bus_socket; \
    systemctl is-active --quiet bluetooth.service; \
    timeout 5 bluetoothctl show | grep -Fq 'Powered: yes'"
  echo "    BlueZ is active and the Bluetooth controller is powered"
}

verify_webauthn_bridge_runtime() {
  local svc=$1
  local container
  case "$svc" in
    ai-backup-webauthn-bridge-chatgpt) container=ratatoskr-ai-backup-webauthn-bridge-chatgpt ;;
    ai-backup-webauthn-bridge-claude) container=ratatoskr-ai-backup-webauthn-bridge-claude ;;
    *) return 0 ;;
  esac

  echo "==> Verifying filtered BlueZ D-Bus bridge for ${svc##*-}"
  ssh "$RASPI_HOST" "set -eu; \
    CID=\$(docker inspect --format '{{.Id}}' '${container}'); \
    [ \"\$(docker inspect --format '{{.HostConfig.NetworkMode}}' \"\$CID\")\" = none ]; \
    docker exec \"\$CID\" /usr/local/bin/ai-backup-webauthn-healthcheck.sh; \
    docker exec \"\$CID\" dbus-send \
      --bus=unix:path=/run/host-dbus/system_bus_socket \
      --type=method_call --print-reply --reply-timeout=3000 \
      --dest=org.freedesktop.hostname1 /org/freedesktop/hostname1 \
      org.freedesktop.DBus.Peer.Ping >/dev/null; \
    if DENIED_OUTPUT=\$(docker exec \"\$CID\" dbus-send \
      --bus=unix:path=/run/ratatoskr-dbus/system_bus_socket \
      --type=method_call --print-reply --reply-timeout=3000 \
      --dest=org.freedesktop.hostname1 /org/freedesktop/hostname1 \
      org.freedesktop.DBus.Peer.Ping 2>&1); then \
      echo 'ERROR: filtered D-Bus bridge allowed a non-BlueZ service' >&2; exit 1; \
    fi; \
    if ! printf '%s\n' \"\$DENIED_OUTPUT\" | grep -Fq \
      'Error org.freedesktop.DBus.Error.ServiceUnknown:'; then \
      echo 'ERROR: unexpected D-Bus policy response' >&2; \
      printf '%s\n' \"\$DENIED_OUTPUT\" >&2; \
      exit 1; \
    fi"
  echo "    org.bluez is reachable; unrelated system-bus services are denied"
}

verify_headed_browser_runtime() {
  local svc=$1
  local smoke_seed
  case "$svc" in
    cloakbrowser-reauth-chatgpt) smoke_seed=deadbeef0001 ;;
    cloakbrowser-reauth-claude) smoke_seed=deadbeef0002 ;;
    *) return 0 ;;
  esac

  echo "==> Verifying headed Chromium/X11 runtime for ${svc}"
  if ! ssh "$RASPI_HOST" "set -u; \
    CID=\$(docker inspect --format '{{.Id}}' 'ratatoskr-${svc}' 2>/dev/null); \
    STATUS=0; \
    docker exec \"\$CID\" python -c \"import json,urllib.request; url='http://localhost:9222/json/version?fingerprint=${smoke_seed}&timezone=UTC&locale=en-US'; data=json.load(urllib.request.urlopen(url, timeout=20)); assert data.get('webSocketDebuggerUrl'), data\" || STATUS=1; \
    ARGS=\$(docker exec \"\$CID\" ps -eo args 2>/dev/null) || STATUS=1; \
    printf '%s\\n' \"\$ARGS\" | grep '[c]hrome' >/dev/null || STATUS=1; \
    if printf '%s\\n' \"\$ARGS\" | grep '[c]hrome' | grep -F -- '--headless' >/dev/null; then STATUS=1; fi; \
    printf '%s\\n' \"\$ARGS\" | grep '[c]hrome' | grep -F -- '--ozone-platform=x11' >/dev/null || STATUS=1; \
    docker exec \"\$CID\" python -c \"import os; assert os.environ.get('DBUS_SYSTEM_BUS_ADDRESS') == 'unix:path=/run/ratatoskr-dbus/system_bus_socket'; assert os.path.exists('/run/ratatoskr-dbus/system_bus_socket')\" || STATUS=1; \
    docker exec \"\$CID\" dbus-send --bus=unix:path=/run/ratatoskr-dbus/system_bus_socket --type=method_call --print-reply --reply-timeout=3000 --dest=org.bluez / org.freedesktop.DBus.ObjectManager.GetManagedObjects >/dev/null || STATUS=1; \
    docker exec \"\$CID\" sh -c \"pkill -TERM -f '[/]chrome' || true; i=0; while pgrep -f '[/]chrome' >/dev/null && [ \\\$i -lt 50 ]; do i=\\\$((i + 1)); sleep 0.1; done; if pgrep -f '[/]chrome' >/dev/null; then pkill -KILL -f '[/]chrome' || true; fi; ! pgrep -f '[/]chrome' >/dev/null\" || STATUS=1; \
    [ \"\$STATUS\" -eq 0 ]"; then
    diagnose_service_health "$svc"
    return 1
  fi
  echo "    ${svc} launched headed Chromium on X11 and removed its disposable smoke process"
}

retire_legacy_qdrant_container() {
  echo "==> Checking for a legacy Compose Qdrant container on ${RASPI_HOST}"
  ssh "$RASPI_HOST" "set -eu; \
    CID=\$(docker inspect --format '{{.Id}}' ratatoskr-qdrant 2>/dev/null || true); \
    if [ -z \"\$CID\" ]; then \
      echo '    no legacy Qdrant container found'; \
      exit 0; \
    fi; \
    PROJECT=\$(docker inspect --format '{{ index .Config.Labels \"com.docker.compose.project\" }}' \"\$CID\"); \
    SERVICE=\$(docker inspect --format '{{ index .Config.Labels \"com.docker.compose.service\" }}' \"\$CID\"); \
    if [ \"\$PROJECT\" != '${COMPOSE_PROJECT}' ] || [ \"\$SERVICE\" != 'qdrant' ]; then \
      echo 'ERROR: ratatoskr-qdrant is not the expected Compose-managed legacy container; refusing to remove it' >&2; \
      exit 1; \
    fi; \
    if ! systemctl is-active --quiet qdrant.service; then \
      echo 'ERROR: native qdrant.service is not active; preserving the legacy container' >&2; \
      exit 1; \
    fi; \
    if ! curl --fail --silent --show-error --max-time 5 http://127.0.0.1:6333/readyz >/dev/null; then \
      echo 'ERROR: native Qdrant readiness check failed; preserving the legacy container' >&2; \
      exit 1; \
    fi; \
    docker rm -f \"\$CID\" >/dev/null; \
    echo '    removed legacy Qdrant container; its named volume was preserved'"
}

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
  if is_pinned_browser_service "$svc"; then
    latest_tag=$PINNED_CLOAKBROWSER_IMAGE
  fi
  local attempt verdict
  for attempt in 1 2 3; do
    # `|| true`: a transient ssh drop during recreate must not abort under
    # `set -e`; the verify below (NO_CONTAINER / MISMATCH) drives the retry.
    ssh "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && ${COMPOSE_RUN[*]} up -d --no-build --no-deps --force-recreate ${svc}" || true
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
  printf "%s\n" "$metrics" | ssh "$RASPI_HOST" "docker run --rm -i --user 0 --entrypoint sh -v ${COMPOSE_PROJECT}_pg_backup_metrics:/textfile '${latest_tag}' -c 'cat > /textfile/ratatoskr_deploy_${svc}.prom'"
}

verify_reader_release_metadata() {
  local expected_backend=$GIT_SHA
  local expected_frontend=$FRONTEND_SHA
  local payload running_revision
  if [[ $ROLLBACK -eq 1 ]]; then
    expected_backend=$(remote_image_label "${COMPOSE_PROJECT}-mobile-api:latest" "org.opencontainers.image.revision")
    expected_frontend=$(ssh "$RASPI_HOST" "docker exec ratatoskr-mobile-api cat /app/app/static/web/.source-commit")
  fi
  echo "==> Verifying reader release metadata"
  for svc in "${READER_RELEASE_SERVICES[@]}"; do
    running_revision=$(ssh "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && \
      CID=\$(${COMPOSE_RUN[*]} ps -q ${svc} 2>/dev/null); \
      [ -n \"\$CID\" ] && docker inspect \"\$CID\" --format '{{ index .Config.Labels \"org.opencontainers.image.revision\" }}'")
    if [[ "$running_revision" != "$expected_backend" ]]; then
      echo "ERROR: running ${svc} revision '${running_revision:-<none>}' does not match '${expected_backend}'" >&2
      exit 1
    fi
    echo "    ${svc}=${running_revision}"
  done
  payload=$(ssh "$RASPI_HOST" "curl --fail --silent --show-error --max-time 10 http://127.0.0.1:18000/v1/meta")
  META_PAYLOAD="$payload" \
  EXPECTED_BACKEND_SHA="$expected_backend" \
  EXPECTED_FRONTEND_SHA="$expected_frontend" \
    python3 - <<'PY'
import json
import os

data = json.loads(os.environ["META_PAYLOAD"])["data"]
expected_backend = os.environ["EXPECTED_BACKEND_SHA"]
expected_frontend = os.environ["EXPECTED_FRONTEND_SHA"]
capability = "summaries.content-backfill.v1"
if data.get("backendRevision") != expected_backend:
    raise SystemExit(
        f"backend revision mismatch: {data.get('backendRevision')!r} != {expected_backend!r}"
    )
if data.get("frontendRevision") != expected_frontend:
    raise SystemExit(
        f"frontend revision mismatch: {data.get('frontendRevision')!r} != {expected_frontend!r}"
    )
if capability not in data.get("capabilities", []):
    raise SystemExit(f"required capability missing: {capability}")
print(f"    backend={expected_backend} frontend={expected_frontend} capability={capability}")
PY
}

verify_scraper_runtime() {
  echo "==> Verifying scraper runtime dependencies"
  for container in ratatoskr-bot ratatoskr-mobile-api; do
    ssh "$RASPI_HOST" "docker exec ${container} python -c 'import os; from playwright.sync_api import sync_playwright; p = sync_playwright().start(); executable = p.chromium.executable_path; assert executable.startswith(\"/ms-playwright\"), executable; assert os.path.isfile(executable) and os.access(executable, os.X_OK), executable; p.stop(); print(executable)'"
  done
  ssh "$RASPI_HOST" "docker exec ratatoskr-mobile-api python -c 'import fitz; print(fitz.__doc__.splitlines()[0])'"
}

if [[ $MIGRATE_ONLY -eq 1 ]]; then
  run_remote_migrations
elif [[ $RESTART -eq 1 ]]; then
  retire_legacy_qdrant_container
  verify_webauthn_host
  verify_pinned_cloakbrowser_image
  for svc in "${SERVICES[@]}"; do
    if [[ $ROLLBACK -eq 1 ]]; then
      if is_pinned_browser_service "$svc"; then
        echo "ERROR: rollback is not supported for digest-pinned CloakBrowser services" >&2
        exit 2
      fi
      rollback_service_image "$svc"
    else
      if ! is_pinned_browser_service "$svc"; then
        tag_running_image_as_previous "$svc"
      fi
    fi
    echo "==> Restarting ${svc} on ${RASPI_HOST} (project: ${COMPOSE_PROJECT})"
    restart_service_verified "$svc"

    # Workaround for a `compose up --no-deps --force-recreate` quirk observed
    # 2026-05-24: mobile-api ended up attached to only the external
    # `firecrawl_internal` network, with `docker_default` dropped. A network
    # connect errors with "already exists" when correctly attached, so `|| true`
    # keeps this idempotent across services that may or may not need the default
    # network. The explicit service alias is essential: Prometheus and status
    # probes address exporters by Compose service name.
    if ! is_isolated_reauth_service "$svc"; then
      echo "==> Ensuring ${svc} is attached to docker_default"
      ssh "$RASPI_HOST" "cd ${RASPI_REMOTE_PATH} && \
        CID=\$(${COMPOSE_RUN[*]} ps -q ${svc} 2>/dev/null) && \
        [ -n \"\$CID\" ] && \
        docker network connect --alias '${svc}' docker_default \"\$CID\" 2>/dev/null \
        && echo '    attached docker_default' \
        || echo '    docker_default already attached or not declared'"
    fi
    ensure_mobile_api_control_networks "$svc"
    ensure_reauth_browser_networks "$svc"
    wait_for_service_health "$svc"
    verify_webauthn_bridge_runtime "$svc"
    if is_pinned_browser_service "$svc"; then
      verify_headed_browser_runtime "$svc"
    fi
    if ! is_pinned_browser_service "$svc"; then
      write_deploy_metrics "$svc"
    fi
  done
  if [[ $release_group_requested -eq 1 ]]; then
    verify_reader_release_metadata
    verify_scraper_runtime
  fi
else
  echo "==> Skipping restart (--no-restart). To start manually on the Pi:"
  for svc in "${SERVICES[@]}"; do
    echo "    ssh ${RASPI_HOST} 'cd ${RASPI_REMOTE_PATH} && ${COMPOSE_RUN[*]} up -d --no-build --no-deps --force-recreate ${svc}'"
  done
fi

echo "==> Done."
