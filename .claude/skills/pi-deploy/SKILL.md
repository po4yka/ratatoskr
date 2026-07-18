---
name: pi-deploy
description: Build, ship, and restart the Ratatoskr container on the Raspberry Pi via cross-platform Docker image streaming. Trigger keywords -- pi deploy, raspi, raspberry pi, pi-deploy, ship to pi, deploy production, build-and-deploy-pi.
version: 1.0.0
allowed-tools: Bash, Read
---

# Pi Deployment

Deploy Ratatoskr to the Raspberry Pi by building the `linux/arm64` image on the Mac, streaming it over SSH, and restarting via compose. Migration application is a separate explicit operator step. The Pi never runs `docker build`.

## Prerequisites

- `ssh raspi` works (SSH config alias for the Pi)
- `~/ratatoskr` exists on the Pi (or override with `RASPI_REMOTE_PATH=...`)
- Docker buildx is set up locally for cross-platform builds

## Commands

```bash
# Standard: build + ship + restart `ratatoskr`
make pi-deploy

# Migration dry-run / explicit apply
make pi-migrate
make pi-migrate APPLY=1

# Roll back one service to its retained previous image
make pi-rollback SERVICE=ratatoskr

# Ship the mobile-api image instead of the bot
make pi-deploy SERVICE=mobile-api

# Ship the PostgreSQL backup sidecar and run its startup backup
make pi-deploy SERVICE=pg-backup

# Ship all production app services plus the backup sidecar
make pi-deploy-all

# Full rebuild (after Dockerfile or dependency changes)
make pi-deploy-no-cache

# Ship without restarting on the Pi (manual restart later)
make pi-build-only

# Full flag/env coverage
bash tools/scripts/build-and-deploy-pi.sh --help
```

## The Image-Name Footgun

CLAUDE.md flags this explicitly: there are two different image names in play.

| Built by | Image tag |
| -------- | --------- |
| `make docker-deploy` (legacy) | `ratatoskr:latest` |
| `docker compose build` | `ratatoskr-ratatoskr` (compose prefixes the project name) |

`docker compose up` uses `ratatoskr-ratatoskr`. If you build with `docker build -t ratatoskr:latest` and then run `docker compose up`, your code changes do NOT take effect because compose pulls the older `ratatoskr-ratatoskr` image.

**Always deploy via `make pi-deploy` (or `docker compose build`)**, not `docker build` directly.

## The Migration Footgun

The Pi restart path recreates app containers with `--no-deps` so it does not disturb Postgres, Redis, or Qdrant. Migrations are not applied as an automatic restart side effect. Run `make pi-migrate` first to render the Alembic SQL dry-run, then `make pi-migrate APPLY=1` when you intentionally want to mutate the schema. App containers run `python -m app.cli.migrate_db --check` at startup and fail cleanly if the database is not at Alembic head.

## Rollback

`make pi-deploy` tags the currently running service image as `<project>-<service>:previous` before recreating the container. Use `make pi-rollback SERVICE=<service>` to swap `:latest` and `:previous`, recreate the service, and update the `ratatoskr_deploy_version_info` node-exporter textfile metric. Rollback does not run migrations.

## The Bind-Mount Footgun

The base compose file at `ops/docker/docker-compose.yml` does NOT bind-mount `../../app` over the image. The shipped image is the single source of truth for app code. Do NOT re-add an app bind mount to the base compose file -- it would silently mask `make pi-deploy`.

For LOCAL hot-reload (Mac only, never on the Pi), use the dev overlay:

```bash
docker compose -f ops/docker/docker-compose.yml -f ops/docker/docker-compose.dev.yml up -d ratatoskr
```

## Verifying the Deploy

After `make pi-deploy` completes:

```bash
# Image hash on the Pi
ssh raspi 'docker images ratatoskr-ratatoskr --format "{{.ID}} {{.CreatedSince}}"'

# Container is up and healthy
ssh raspi 'docker ps --filter name=ratatoskr --format "{{.Names}} {{.Status}}"'

# Tail logs for the bot
ssh raspi 'docker logs --tail 50 ratatoskr'

# Or for mobile-api
ssh raspi 'docker logs --tail 50 ratatoskr-mobile-api'
```

## Common Failure Modes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Build hangs at `[platform linux/arm64]` | buildx builder missing | `docker buildx create --use --name multi && docker buildx inspect --bootstrap` |
| `ssh raspi` prompts for password | SSH key not propagated | `ssh-copy-id raspi` |
| Image streams but Pi can't load it | Disk full on Pi | `ssh raspi 'docker system df'` and prune |
| Restart succeeds but code unchanged | Used `docker build` (image-name footgun) | Re-run with `make pi-deploy` |
| Pi runs old image after restart | Compose cached the previous image ref | `ssh raspi 'docker compose -f ~/ratatoskr/ops/docker/docker-compose.yml up -d --force-recreate'` |
| New container exits immediately after deploy | Database is not at Alembic head | Run `make pi-migrate` to inspect SQL, then `make pi-migrate APPLY=1`, or `make pi-rollback SERVICE=<service>` |

## Key Files

- **Script**: `tools/scripts/build-and-deploy-pi.sh` (passes flags + env)
- **Makefile targets**: `pi-deploy`, `pi-deploy-no-cache`, `pi-build-only`, `pi-deploy SERVICE=mobile-api`
- **Base compose**: `ops/docker/docker-compose.yml`
- **Dev overlay (Mac only)**: `ops/docker/docker-compose.dev.yml`
- **Dockerfile**: `ops/docker/Dockerfile`

## Important Notes

- The Pi is single-tenant production -- avoid `docker compose down` during user sessions if possible; `up -d --force-recreate` is gentler.
- Image streaming uses `docker save | ssh raspi docker load` -- it's bandwidth-heavy. Don't run it from a slow network.
- `RASPI_REMOTE_PATH` overrides the assumed `~/ratatoskr` location.
- Mobile API, the bot, and `pg-backup` are separate images. `make pi-deploy-all`
  includes all app processes plus `pg-backup`; a targeted deploy restarts only
  the selected service.
- The Pi keeps its own `.env` -- never commit Pi-specific secrets locally.
